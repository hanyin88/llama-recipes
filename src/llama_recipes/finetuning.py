# Copyright (c) Meta Platforms, Inc. and affiliates.
# This software may be used and distributed according to the terms of the Llama 2 Community License Agreement.

import os
from pkg_resources import packaging

import fire
import random
import torch
import torch.optim as optim
from peft import get_peft_model, prepare_model_for_int8_training
from torch.distributed.fsdp import (
    FullyShardedDataParallel as FSDP,
)
from torch.distributed.fsdp.fully_sharded_data_parallel import CPUOffload
from torch.optim.lr_scheduler import StepLR
from transformers import (
    LlamaForCausalLM,
    LlamaTokenizer,
    LlamaConfig,
    get_cosine_schedule_with_warmup,
)
from transformers.models.llama.modeling_llama import LlamaDecoderLayer

from llama_recipes.configs import fsdp_config as FSDP_CONFIG
from llama_recipes.configs import train_config as TRAIN_CONFIG
from llama_recipes.data.concatenator import ConcatDataset
from llama_recipes.policies import AnyPrecisionAdamW, apply_fsdp_checkpointing

from llama_recipes.utils import fsdp_auto_wrap_policy
from llama_recipes.utils.config_utils import (
    update_config,
    generate_peft_config,
    generate_dataset_config,
    get_dataloader_kwargs,
)
from llama_recipes.utils.dataset_utils import get_preprocessed_dataset

from llama_recipes.utils.train_utils import (
    train,
    freeze_transformer_layers,
    setup,
    setup_environ_flags,
    clear_gpu_cache,
    print_model_size,
    get_policies
)

from pprint import pprint


def main(**kwargs):
    # Update the configuration for the training and sharding process
    train_config, fsdp_config = TRAIN_CONFIG(), FSDP_CONFIG()
    update_config((train_config, fsdp_config), **kwargs)

    pprint(f"--> Training Config: {train_config} \n")
    pprint(f"--> FSDP Config: {fsdp_config} \n")

    # Set the seeds for reproducibility
    torch.cuda.manual_seed(train_config.seed)
    torch.manual_seed(train_config.seed)
    random.seed(train_config.seed)

    if train_config.enable_fsdp:
        setup()
        # torchrun specific
        local_rank = int(os.environ["LOCAL_RANK"])
        rank = int(os.environ["RANK"])
        world_size = int(os.environ["WORLD_SIZE"])

    if torch.distributed.is_initialized():
        torch.cuda.set_device(local_rank)
        clear_gpu_cache(local_rank)
        setup_environ_flags(rank)

    # Load the pre-trained model and setup its configuration
    use_cache = False if train_config.enable_fsdp else None
    if train_config.enable_fsdp and train_config.low_cpu_fsdp:
        """
        for FSDP, we can save cpu memory by loading pretrained model on rank0 only.
        this avoids cpu oom when loading large models like llama 70B, in which case
        model alone would consume 2+TB cpu mem (70 * 4 * 8). This will add some comms
        overhead and currently requires latest nightly.
        """
        v = packaging.version.parse(torch.__version__)
        verify_latest_nightly = v.is_devrelease and v.dev >= 20230701
        if not verify_latest_nightly:
            raise Exception("latest pytorch nightly build is required to run with low_cpu_fsdp config, "
                            "please install latest nightly.")
        if rank == 0:
            model = LlamaForCausalLM.from_pretrained(
                train_config.model_name,
                load_in_8bit=True if train_config.quantization else None,
                device_map="auto" if train_config.quantization else None,
                use_cache=use_cache,
            )
        else:
            llama_config = LlamaConfig.from_pretrained(train_config.model_name)
            llama_config.use_cache = use_cache
            with torch.device("meta"):
                model = LlamaForCausalLM(llama_config)

    else:
        model = LlamaForCausalLM.from_pretrained(
            train_config.model_name,
            load_in_8bit=True if train_config.quantization else None,
            device_map="auto" if train_config.quantization else None,
            use_cache=use_cache,
        )
    if train_config.enable_fsdp and train_config.use_fast_kernels:
        """
        For FSDP and FSDP+PEFT, setting 'use_fast_kernels' will enable
        using of Flash Attention or Xformer memory-efficient kernels
        based on the hardware being used. This would speed up fine-tuning.
        """
        try:
            from optimum.bettertransformer import BetterTransformer
            model = BetterTransformer.transform(model)
        except ImportError:
            print("Module 'optimum' not found. Please install 'optimum' it before proceeding.")

    # Load the tokenizer and add special tokens
    tokenizer = LlamaTokenizer.from_pretrained(train_config.model_name)
    tokenizer.pad_token_id = tokenizer.eos_token_id

    print_model_size(model, train_config, rank if train_config.enable_fsdp else 0)

    # Prepare the model for int8 training if quantization is enabled
    if train_config.quantization:
        model = prepare_model_for_int8_training(model)

    # Convert the model to bfloat16 if fsdp and pure_bf16 is enabled
    if train_config.enable_fsdp and fsdp_config.pure_bf16:
        model.to(torch.bfloat16)


# Decide to try the route of using PeftModel.from_pretrained to load the model, 
# as suggested by PEFT post: https://github.com/huggingface/peft/issues/184
# current logic did not consider contrinue training from a full model check point!
    if train_config.use_peft:
        if train_config.resume_from_checkpoint:
            from peft import PeftModel
            # mark_only_lora_as_trainable did not change trainable arguments, therefore i did not call it
            # import loralib as lora
             # only LoRA model - LoRA config above has to fit
            if os.path.exists(train_config.resume_from_checkpoint):
                print(f"Restarting from {train_config.resume_from_checkpoint}")
                model = PeftModel.from_pretrained(model, train_config.resume_from_checkpoint, is_trainable=True)
                # lora.mark_only_lora_as_trainable(model)

                pprint(f"--> PEFT Config from loaded check point: {model.peft_config}")
            else:
                print(f"Checkpoint {train_config.resume_from_checkpoint} not found")

        else:
            peft_config = generate_peft_config(train_config, kwargs)
            pprint(f"--> PEFT Config: {peft_config}")

            model = get_peft_model(model, peft_config)  

        ## code below is for loading PEFT model from checkpoint
        ## run into error of Attempting to deserialize object on CUDA device 1 but torch.cuda.device_count() is 1. 
        ## copied from alpaca-lora, but only consider the case of loading LORA checkpoint
        ## Need to look into how to load full model from checkpoint in the future if doing full model training!
            # if train_config.resume_from_checkpoint:
            #     from peft import set_peft_model_state_dict

            #     # Check the available weights and load them
            #     checkpoint_name = os.path.join(
            #             train_config.resume_from_checkpoint, "adapter_model.bin"
            #         )  # only LoRA model - LoRA config above has to fit
            #     # The two files above have a different name depending on how they were saved, but are actually the same.
            #     if os.path.exists(checkpoint_name):
            #         print(f"Restarting from {checkpoint_name}")
            #         # adapters_weights = torch.load(checkpoint_name)
            #         # set_peft_model_state_dict(model, adapters_weights)
            #     else:
            #         print(f"Checkpoint {checkpoint_name} not found")


        model.print_trainable_parameters()


    

    #setting up FSDP if enable_fsdp is enabled
    if train_config.enable_fsdp:
        if not train_config.use_peft and train_config.freeze_layers:

            freeze_transformer_layers(train_config.num_freeze_layers)

        mixed_precision_policy, wrapping_policy = get_policies(fsdp_config, rank)
        my_auto_wrapping_policy = fsdp_auto_wrap_policy(model, LlamaDecoderLayer)

        model = FSDP(
            model,
            auto_wrap_policy= my_auto_wrapping_policy if train_config.use_peft else wrapping_policy,
            cpu_offload=CPUOffload(offload_params=True) if fsdp_config.fsdp_cpu_offload else None,
            mixed_precision=mixed_precision_policy if not fsdp_config.pure_bf16 else None,
            sharding_strategy=fsdp_config.sharding_strategy,
            device_id=torch.cuda.current_device(),
            limit_all_gathers=True,
            sync_module_states=train_config.low_cpu_fsdp,
            param_init_fn=lambda module: module.to_empty(device=torch.device("cuda"), recurse=False)
            if train_config.low_cpu_fsdp and rank != 0 else None,
        )
        if fsdp_config.fsdp_activation_checkpointing:
            apply_fsdp_checkpointing(model)
    elif not train_config.quantization and not train_config.enable_fsdp:
        model.to("cuda")

    dataset_config = generate_dataset_config(train_config, kwargs)

     # Load and preprocess the dataset for training and validation
    dataset_train = get_preprocessed_dataset(
        tokenizer,
        dataset_config,
        split="train",
    )

    if not train_config.enable_fsdp or rank == 0:
        print(f"--> Training Set Length = {len(dataset_train)}")

    dataset_val = get_preprocessed_dataset(
        tokenizer,
        dataset_config,
        split="test",
    )
    if not train_config.enable_fsdp or rank == 0:
            print(f"--> Validation Set Length = {len(dataset_val)}")


# For packing strategy, this concatenate step takes some time when dataset is large
    if train_config.batching_strategy == "packing":
        dataset_train = ConcatDataset(dataset_train, chunk_size=train_config.context_length)

# For padding strategy, this get_kwargs step takes some time when dataset is large
    train_dl_kwargs = get_dataloader_kwargs(train_config, dataset_train, tokenizer, "train")

    # Create DataLoaders for the training and validation dataset
    train_dataloader = torch.utils.data.DataLoader(
        dataset_train,
        num_workers=train_config.num_workers_dataloader,
        pin_memory=True,
        **train_dl_kwargs,
    )

    eval_dataloader = None
    if train_config.run_validation:
        if train_config.batching_strategy == "packing":
            dataset_val = ConcatDataset(dataset_val, chunk_size=train_config.context_length)

        val_dl_kwargs = get_dataloader_kwargs(train_config, dataset_val, tokenizer, "val")

        eval_dataloader = torch.utils.data.DataLoader(
            dataset_val,
            num_workers=train_config.num_workers_dataloader,
            pin_memory=True,
            **val_dl_kwargs,
        )

    # Initialize the optimizer and learning rate scheduler
    if fsdp_config.pure_bf16 and fsdp_config.optimizer == "anyprecision":
        optimizer = AnyPrecisionAdamW(
            model.parameters(),
            lr=train_config.lr,
            momentum_dtype=torch.bfloat16,
            variance_dtype=torch.bfloat16,
            use_kahan_summation=False,
            weight_decay=train_config.weight_decay,
        )
    else:
        optimizer = optim.AdamW(
            model.parameters(),
            lr=train_config.lr,
            weight_decay=train_config.weight_decay,
        )

    if train_config.use_cosine_scheduler:
        total_length = len(train_dataloader)//train_config.gradient_accumulation_steps
        scheduler = get_cosine_schedule_with_warmup(optimizer, train_config.warmup_steps, total_length * train_config.num_epochs)
    else:
        scheduler = StepLR(optimizer, step_size=1, gamma=train_config.gamma)
    # scheduler = StepLR(optimizer, step_size=1, gamma=train_config.gamma)

    if train_config.use_wandb:
        import wandb
        # from datetime import datetime
        # now = datetime.now()
        # date_string = now.strftime("%B-%d-%H-%M")

        wandb_run = wandb.init(
            # Set the project where this run will be logged
            project = train_config.wandb_project,
            # name = f"{train_config.run_name}-{date_string}-{local_rank}",
            name = f"{train_config.run_name}_{local_rank}",
            group = train_config.run_group)
            # Track hyperparameters and run metadata)

        # os.environ["WANDB_PROJECT"] = train_config.wandb_project
        # train_config.report_to = "wandb"
        
        # 
        # now = datetime.now()
        # date_string = now.strftime("%B-%d-%H-%M")
        # train_config.run_name = f"{train_config.run_name}-{date_string}"
    else:
        wandb_run = None

    # Start the training process
    results = train(
        model,
        train_dataloader,
        eval_dataloader,
        tokenizer,
        optimizer,
        scheduler,
        train_config.gradient_accumulation_steps,
        train_config,
        fsdp_config if train_config.enable_fsdp else None,
        local_rank if train_config.enable_fsdp else None,
        rank if train_config.enable_fsdp else None,
        wandb_run,
        train_config.use_cosine_scheduler # if using cosine scheduler, step_scheduler will automatically be Trune in train_utils.py! This is smart
    )
    if not train_config.enable_fsdp or rank==0:
        [print(f'Key: {k}, Value: {v}') for k, v in results.items()]

if __name__ == "__main__":
    fire.Fire(main)
