from configparser import NoSectionError
from gfn.envs import HyperGrid
import torch
import os
from gfn.parametrizations import TBParametrization
from gfn.estimators import LogitPFEstimator, LogitPBEstimator, LogZEstimator
from gfn.samplers import LogitPFActionsSampler
from gfn.containers import ReplayBuffer, Transitions
import math


def make_tb_parametrization(env, PB, load_from=None):
    """
    It creates a TrajectoryBalance parametrization

    :param env: the environment we're working in
    :param load_from: If you want to load a previously saved model, you can pass the path to the saved
    model here
    :return: A parametrization of the model.
    """
    logZ_tensor = torch.tensor(0.0)
    logZ = LogZEstimator(logZ_tensor)
    logit_PF = LogitPFEstimator(env=env, module_name="NeuralNet")
    logit_PB = LogitPBEstimator(
        env=env,
        module_name="NeuralNet" if PB in ["learnable", "tied"] else "Uniform",
        torso=logit_PF.module.torso if PB == "tied" else None,
    )
    parametrization = TBParametrization(logit_PF, logit_PB, logZ)
    if load_from is not None:
        parametrization.load_state_dict(load_from)
    return parametrization


def make_buffer(env, capacity, load_from=None):
    """
    It creates a buffer for the environment. If load_from is not None, it loads the buffer from the
    path specified by load_from.

    :param env: the environment
    :param capacity: the capacity of the buffer
    :param load_from: the path to load the buffer from
    :return: the buffer
    """
    buffer = ReplayBuffer(env, capacity, objects="trajectories")
    if load_from is not None:
        buffer.load(load_from)
    return buffer


def make_optimizers(
    parametrization,
    lr,
    lr_PB,
    lr_Z,
    schedule,
    scheduler_type="linear",  # TODO: FIX
    load_from=None,
):
    """
    It creates two optimizers, one for the parameters of the model and one for the log-partition
    function, and two schedulers, one for each optimizer

    :param parametrization: the parametrization of the model
    :param mode: "tb" or "tb_Z"
    :param lr: learning rate for the parameters
    :param lr_Z: learning rate for the logZ parameter
    :param schedule: the learning rate decay schedule, gamma if "linear" which is MultiStepLR, or
    :param load_from: the directory to load the optimizers from. If None, then the optimizers are initialized from
    scratch
    :return: optimizer, optimizer_Z, scheduler, scheduler_Z
    """
    params_pf = parametrization.logit_PF.module.parameters()
    optimizer_pf = torch.optim.Adam(params_pf, lr=lr)
    optimizer_pb = None
    if parametrization.logit_PB.module_name == "NeuralNet":
        params_pb = parametrization.logit_PB.module.parameters()
        optimizer_pb = torch.optim.Adam(params_pb, lr=lr_PB)
    optimizer_Z = torch.optim.Adam([parametrization.logZ.tensor], lr=lr_Z)
    scheduler_pb = None
    if scheduler_type == "linear":
        scheduler_pf = torch.optim.lr_scheduler.MultiStepLR(
            optimizer_pf, milestones=list(range(2000, 100000, 2000)), gamma=schedule
        )
        if optimizer_pb is not None:
            scheduler_pb = torch.optim.lr_scheduler.MultiStepLR(
                optimizer_pb, milestones=list(range(2000, 100000, 2000)), gamma=schedule
            )
        scheduler_Z = torch.optim.lr_scheduler.MultiStepLR(
            optimizer_Z, milestones=list(range(2000, 100000, 2000)), gamma=schedule
        )
    # elif (
    #     scheduler_type == "cosine"
    # ):  #  FALSE -- FIX THIS ## TODO: MAYBE NOT THAT IMPORTANT TO HAVE A COSINE SCHEDULER HERE - JUST USE THE MULTISTEP ONE (SO NO ARGUMENT SCHEDULER_TYPE FOR THIS FUNCTION)
    #     scheduler_pf = torch.optim.lr_scheduler.LambdaLR(
    #         optimizer_pf, lambda i: cosine_annealing_schedule(i, 1, schedule, 2000)
    #     )
    #     if optimizer_pb is not None:
    #         scheduler_pb = torch.optim.lr_scheduler.LambdaLR(
    #             optimizer_pb, lambda i: cosine_annealing_schedule(i, 1, schedule, 2000)
    #         )
    #     scheduler_Z = torch.optim.lr_scheduler.LambdaLR(
    #         optimizer_Z, lambda i: cosine_annealing_schedule(i, 1, schedule, 2000)
    #     )
    else:
        raise ValueError("Unknown scheduler type")
    if load_from is not None:
        optimizer_pf.load_state_dict(
            torch.load(os.path.join(load_from, "optimizer_pf.pt"))
        )
        if optimizer_pb is not None:
            optimizer_pb.load_state_dict(
                torch.load(os.path.join(load_from, "optimizer_pb.pt"))
            )
        optimizer_Z.load_state_dict(
            torch.load(os.path.join(load_from, "optimizer_Z.pt"))
        )
    return (
        optimizer_pf,
        optimizer_pb,
        optimizer_Z,
        scheduler_pf,
        scheduler_pb,
        scheduler_Z,
    )


def get_metadata(load_from=None):
    if load_from is not None:
        with open(os.path.join(load_from, "metadata.txt"), "r") as f:
            lines = f.readlines()
            iteration = int(lines[0].split(":")[1].strip())
            wandb_id = lines[1].split(":")[1].strip()
    else:
        iteration = 0
        wandb_id = None
    return (iteration, wandb_id)


def cosine_annealing_schedule(iteration, init, final, last_update):
    """
    A cosine annealing schedule that starts at init and ends at final after last_update iterations
    init is the max value
    """
    if iteration >= last_update:
        return final
    else:
        return final + (init - final) * 0.5 * (
            1 + math.cos(math.pi * iteration / last_update)
        )


def temperature_epsilon_schedule(
    iteration,
    init_temp,
    init_epsilon,
    final_temp,
    final_epsilon,
    last_update,
    scheduler_type="linear",
):
    """
    A temperature and epsilon schedule that starts at init_temp and ends at final_temp after last_update iterations
    """
    if iteration >= last_update:
        return final_temp, final_epsilon
    else:
        if scheduler_type == "linear":
            return (
                init_temp + (final_temp - init_temp) * iteration / last_update,
                init_epsilon + (final_epsilon - init_epsilon) * iteration / last_update,
            )
        elif scheduler_type == "cosine":
            return (
                cosine_annealing_schedule(
                    iteration, init_temp, final_temp, last_update
                ),
                cosine_annealing_schedule(
                    iteration, init_epsilon, final_epsilon, last_update
                ),
            )


def evaluate_trajectories(
    args, parametrization, loss_fn, trajectories, temperature, epsilon
):
    if args.mode == "modified_db":
        transitions = Transitions.from_trajectories(trajectories)
        scores = loss_fn.get_modified_scores(transitions)
        logPF_trajectories, logPB_trajectories = None, None
    else:
        logPF_trajectories, logPB_trajectories, scores = loss_fn.get_scores(
            trajectories
        )
    importance_sampling_weights = 1.0
    on_policy_importance_sampling_weights = 1.0
    baseline = 0.0
    if args.mode in [
        "symmetric_cycles",
        "forward_kl",
        "reverse_kl",
        "rws",
        "reverse_rws",
    ]:
        if args.baseline == "local":
            baseline = scores.mean().detach()
        elif args.baseline == "global":
            baseline = -parametrization.logZ.tensor.detach()
        else:
            baseline = 0.0
        if args.sampling_mode == "off_policy":
            tempered_logPF_trajectories, _ = loss_fn.get_pfs_and_pbs(
                trajectories, temperature=temperature, epsilon=epsilon  # type: ignore
            )
            tempered_logPF_trajectories = tempered_logPF_trajectories.sum(dim=0)
            importance_sampling_weights = torch.exp(
                -tempered_logPF_trajectories + logPF_trajectories  # type: ignore
            ).detach()
        on_policy_importance_sampling_weights = (
            torch.exp(-scores) / torch.exp(-scores).sum()
        ).detach()

    return (
        scores,
        baseline,
        importance_sampling_weights,
        on_policy_importance_sampling_weights,
        logPF_trajectories,
        logPB_trajectories,
    )


def evaluate_loss(
    args,
    parametrization,
    scores,
    baseline,
    importance_sampling_weights,
    on_policy_importance_sampling_weights,
    logPF_trajectories,
    logPB_trajectories,
):
    if args.mode == "tb":
        loss = (scores + parametrization.logZ.tensor).pow(2)
    elif args.mode == "modified_db":
        loss = scores.pow(2)
    elif args.mode == "symmetric_cycles":
        loss = (
            logPF_trajectories
            * (scores.detach() - baseline - on_policy_importance_sampling_weights)
            - logPB_trajectories
            - logPB_trajectories  # type: ignore
            * on_policy_importance_sampling_weights
            * (scores.detach() - baseline)
        )

    elif args.mode == "reverse_kl":
        loss = logPF_trajectories * (scores.detach() - baseline) - logPB_trajectories
    elif args.mode == "reverse_rws":
        loss_pf = logPF_trajectories * (scores.detach() - baseline)
        loss_pb = (
            -on_policy_importance_sampling_weights  # type: ignore
            * logPB_trajectories  # type: ignore
            * (scores.detach() - baseline)
        )
        loss = loss_pf + loss_pb
    elif args.mode == "forward_kl":
        loss = -logPB_trajectories * (scores.detach() - baseline) - logPF_trajectories  # type: ignore
        loss = loss * on_policy_importance_sampling_weights
    elif args.mode == "rws":
        loss_pf = -logPF_trajectories * on_policy_importance_sampling_weights  # type: ignore
        loss_pb = -logPB_trajectories  # type: ignore
        loss = loss_pf + loss_pb
    else:
        raise NotImplementedError("Only TB is implemented for now")

    loss = loss * importance_sampling_weights
    loss = loss.mean()

    return loss


def save(
    parametrization,
    optimizer_pf,
    optimizer_pb,
    optimizer_Z,
    scheduler_pf,
    scheduler_pb,
    scheduler_Z,
    replay_buffer,
    iteration,
    wandb_id,
    save_path,
):
    """
    It saves the model, optimizer, scheduler, and iteration number to a folder

    :param parametrization: the model
    :param optimizer: the optimizer for the parameters of the model
    :param optimizer_Z: optimizer for the latent space
    :param scheduler: the learning rate scheduler
    :param scheduler_Z: the scheduler for the Z optimizer
    :param iteration: the current iteration of the training loop
    :param wandb_id: the id of the run in wandb
    :param save_path: the path to save the model to
    """
    parametrization.save_state_dict(save_path)
    torch.save(optimizer_pf.state_dict(), os.path.join(save_path, "optimizer.pt"))
    torch.save(scheduler_pf.state_dict(), os.path.join(save_path, "scheduler.pt"))
    if optimizer_pf is not None and scheduler_pb is not None:
        torch.save(
            optimizer_pb.state_dict(), os.path.join(save_path, "optimizer_pb.pt")
        )
        torch.save(
            scheduler_pb.state_dict(), os.path.join(save_path, "scheduler_pb.pt")
        )
    torch.save(optimizer_Z.state_dict(), os.path.join(save_path, "optimizer_Z.pt"))
    torch.save(scheduler_Z.state_dict(), os.path.join(save_path, "scheduler_Z.pt"))
    if replay_buffer is not None:
        replay_buffer.save(save_path)
    with open(os.path.join(save_path, "metadata.txt"), "w") as f:
        f.write("iteration: {}\n".format(iteration))
        f.write("wandb_id: {}\n".format(wandb_id))


def deep_iter(data, ix=tuple()):
    "Iterates over a multi-dimensional tensor, copied from https://stackoverflow.com/questions/59332694/replacement-of-nditer-for-numpy-array-for-pytorch-tensor"
    try:
        for i, element in enumerate(data):
            yield from deep_iter(element, ix + (i,))
    except:
        yield ix, data


def get_exact_P_T(env, logit_PF: LogitPFEstimator):
    """This function evaluates the exact terminating state distribution P_T for HyperGrid.
    P_T(s') = u(s') P_F(s_f | s') where u(s') = \sum_{s \in Par(s')}  u(s) P_F(s' | s), and u(s_0) = 1
    """

    grid = env.build_grid()
    ndim = env.ndim
    action_sampler = LogitPFActionsSampler(logit_PF, temperature=1.0)
    probabilities = action_sampler.get_probs(grid)[1]
    u = torch.ones(grid.batch_shape)
    iter_u = list(deep_iter(u))
    for grid_ix, _ in iter_u:
        if grid_ix == (0,) * ndim:
            continue
        else:
            index = tuple(grid_ix)
            parents = [
                index[:i] + (index[i] - 1,) + index[i + 1 :] + (i,)
                for i in range(len(index))
                if index[i] > 0
            ]
            parents = torch.tensor(parents).T.numpy().tolist()
            u[index] = torch.sum(u[parents[:-1]] * probabilities[parents])
    return (u * probabilities[..., -1]).view(-1).detach().cpu()


def JSD(P, Q):
    """Computes the Jensen-Shannon divergence between two distributions P and Q"""
    M = 0.5 * (P + Q)
    return 0.5 * (torch.sum(P * torch.log(P / M)) + torch.sum(Q * torch.log(Q / M)))


def get_validation_info(env, parametrization):
    true_logZ = env.log_partition
    true_dist_pmf = env.true_dist_pmf.cpu()

    logZ = None
    if isinstance(parametrization, TBParametrization):
        logZ = parametrization.logZ.tensor.item()

    P_T = get_exact_P_T(env, parametrization.logit_PF)
    l1_dist = (P_T - true_dist_pmf).abs().mean().item()
    jsd = JSD(P_T, true_dist_pmf).item()

    validation_info = {"l1_dist": l1_dist, "jsd": jsd}
    if logZ is not None:
        validation_info["logZ_error"] = abs(logZ - true_logZ)

    return validation_info
