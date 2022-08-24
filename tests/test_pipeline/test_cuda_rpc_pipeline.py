import os
import argparse

import torch
from torch import nn
import torch.multiprocessing as mp
import torch.distributed.rpc as rpc

from colossalai.pipeline.rpc.PipelineBase import FillDrainPipelineEngine, OneFOneBPipelineEngine


class TestModel(nn.Module):

    def __init__(self, stage_id, actual_stage_num, feat_num, h) -> None:
        super().__init__()
        self.rank = stage_id
        self.is_last_rank = stage_id == actual_stage_num - 1
        self.linear_name = f'linear_{stage_id}'
        if stage_id == 0:
            setattr(self, self.linear_name, nn.Linear(feat_num, h))
        elif stage_id == actual_stage_num - 1:
            setattr(self, self.linear_name, nn.Linear(h, 1))
        else:
            setattr(self, self.linear_name, nn.Linear(h, h))

    def forward(self, x) -> torch.Tensor:
        linear: nn.Module = getattr(self, self.linear_name)
        out: torch.Tensor = linear(x)

        if self.is_last_rank:
            out = out.sum()
        return out


def run_main(args):
    torch.manual_seed(100)

    device = args.device
    stage_num = args.world_size
    chunk = args.chunk
    num_microbatches = args.num_microbatches
    actual_stage_num = stage_num * chunk
    use_interleave = args.use_interleave
    use_checkpoint = args.use_checkpoint

    sample_num = 1024
    feat_num = 10
    h = 10
    batch_size = 1024

    assert sample_num % batch_size == 0
    batch_num = sample_num // batch_size

    input_sample = torch.randn((sample_num, feat_num), device=device)

    module_partitions = [TestModel(pp_rank, actual_stage_num, feat_num, h) for pp_rank in range(actual_stage_num)]

    engine = OneFOneBPipelineEngine(module_partitions=module_partitions,
                                    stage_num=stage_num,
                                    num_microbatches=num_microbatches,
                                    device=device,
                                    chunk=chunk,
                                    use_interleave=use_interleave,
                                    checkpoint=use_checkpoint)

    _ = engine.forward_backward(input_sample)


def run_worker(rank, args):
    os.environ['MASTER_ADDR'] = args.master_addr
    os.environ['MASTER_PORT'] = args.master_port

    # config rpc
    # if cuda is used, set_device_map is a must is configured
    # for cuda is not supported in torch rpc by default
    options = rpc.TensorPipeRpcBackendOptions(num_worker_threads=args.num_worker_threads)

    world_size = args.world_size
    for rank_idx in range(world_size):
        options.set_device_map(f'work{rank_idx}', {rank: rank_idx})

    rpc.init_rpc(name=f'work{rank}', rank=rank, world_size=world_size, rpc_backend_options=options)

    # in rpc mode, only rank 0 is needed to be coded
    if rank == 0:
        run_main(args)
    # barrier here
    rpc.shutdown()


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--world_size', type=int, default=2)
    parser.add_argument('--num_microbatches', type=int, default=2)
    parser.add_argument('--device', type=str, default='cuda')
    parser.add_argument('--chunk', type=int, default=1)
    parser.add_argument('--use_checkpoint', action='store_true')
    parser.add_argument('--use_interleave', action='store_true')
    parser.add_argument('--master_addr', type=str, default='localhost')
    parser.add_argument('--master_port', type=str, default='29020')
    parser.add_argument('--num_worker_threads', type=str, default=128)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    world_size = args.world_size
    assert args.device in ['cpu', 'cuda'], "device must be cpu or cuda!"
    mp.spawn(run_worker, args=(args,), nprocs=world_size)