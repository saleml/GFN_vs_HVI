import os
import subprocess
import stat
from argparse import ArgumentParser

parser = ArgumentParser()
parser.add_argument("--job_name", type=str, default=None)
parser.add_argument("--n_threads_per_task", type=int, default=1)
parser.add_argument("--ntasks_per_node", type=int, default=6)
parser.add_argument("--partition", type=str, default="main")
parser.add_argument("--offset", type=int, default=0)
parser.add_argument("--experiment_name", type=str, default="hvi_paper_smallenvs")
parser.add_argument("--no_cuda", action="store_true", default=False)
parser.add_argument("--failed", action="store_true", default=False)
parser.add_argument("--small", action="store_true", default=False)
parser.add_argument("--sweep", type=str, default=None)

args = parser.parse_args()

wandb_name = args.experiment_name

scratch_path = os.environ["SCRATCH_PATH"]
log_directory = os.path.join(scratch_path, args.experiment_name)
slurm_outputs_directory = os.path.join(log_directory, "slurm_outputs")
if not os.path.exists(slurm_outputs_directory):
    os.makedirs(slurm_outputs_directory)
sbatch_directory = os.path.join(log_directory, "sbatch_scripts")
if not os.path.exists(sbatch_directory):
    os.makedirs(sbatch_directory)

job_name = (
    f"{args.job_name}_{args.offset}"
    if args.job_name is not None
    else f"{args.experiment_name.split('_')[0]}_{args.offset}"
)
if args.failed:
    job_name = f"{job_name}_f"
if args.small:
    job_name = f"{job_name}_s"
if args.sweep is not None:
    job_name = f"{job_name}_{args.sweep}"

output_filename = os.path.join(slurm_outputs_directory, f"{job_name}")

ntasks_per_node = args.ntasks_per_node
partition = args.partition
gres = "gpu:1"
cpus_per_task = 1
mem = "16G"

conda_env = "gfn"

cuda_str = " --no_cuda" if args.no_cuda else ""
failed_str = " --failed_runs" if args.failed else ""
small_str = " --small" if args.small else ""

bash_range = "{1.." + str(args.n_threads_per_task) + "}"
configs_str = f"--task_id=$i --total={args.n_threads_per_task} --offset={args.offset}"

if args.sweep is not None:
    script_to_run = f"wandb agent user_name/{args.experiment_name}/{args.sweep}"
else:
    script_to_run = f"python train.py {configs_str} {failed_str} {cuda_str} {small_str} --wandb={wandb_name}"

sbatch_skeleton = f"""#!/bin/bash
#SBATCH --job-name={job_name}
#SBATCH --output={output_filename}.out
#SBATCH --ntasks-per-node={ntasks_per_node}
#SBATCH --partition={partition}
#SBATCH --gres={gres}
#SBATCH --cpus-per-task={cpus_per_task}
#SBATCH --mem={mem}

module load anaconda/3
conda activate {conda_env}


srun --output={output_filename}-%t.out bash -c 'for i in {bash_range}; do {script_to_run} & done; wait;'
"""

sbatch_target = os.path.join(sbatch_directory, f"{job_name}.sh")

with open(sbatch_target, "w+") as f:
    f.writelines(sbatch_skeleton)

st = os.stat(sbatch_target)
os.chmod(sbatch_target, st.st_mode | stat.S_IEXEC)

subprocess.check_output(f"sbatch {sbatch_target}", shell=True)
