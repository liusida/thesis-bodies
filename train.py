import argparse
import difflib
import importlib
import os
import time
import uuid
import warnings
from collections import OrderedDict
from pprint import pprint

import gym
import numpy as np
import seaborn
import torch as th
import yaml
from stable_baselines3.common.callbacks import CheckpointCallback, EvalCallback
from stable_baselines3.common.noise import NormalActionNoise, OrnsteinUhlenbeckActionNoise
from stable_baselines3.common.preprocessing import is_image_space
from stable_baselines3.common.sb2_compat.rmsprop_tf_like import RMSpropTFLike  # noqa: F401
from stable_baselines3.common.utils import constant_fn, set_random_seed
from stable_baselines3.common.vec_env import DummyVecEnv, VecFrameStack, VecNormalize, VecTransposeImage
from stable_baselines3.common.vec_env.obs_dict_wrapper import ObsDictWrapper

# For custom activation fn
from torch import nn as nn  # noqa: F401 pytype: disable=unused-import

# Register custom envs
import utils.import_envs  # noqa: F401 pytype: disable=import-error
from utils import ALGOS, get_latest_run_id, get_wrapper_class, linear_schedule, make_env
from utils.callbacks import SaveVecNormalizeCallback
from utils.hyperparams_opt import hyperparam_optimization
from utils.noise import LinearNormalActionNoise
from utils.utils import StoreDict, get_callback_class

import arguments

seaborn.set()

import load_dataset

if __name__ == "__main__":  # noqa: C901
    args = arguments.get_train_args()
    if args.powercoeff is None:
        args.powercoeff = [1., 1., 1.]
    
    # Load body dataset
    dataset_name, env_id, train_files, train_params, train_names, test_files, test_params, test_names = load_dataset.load_dataset(
        args.dataset, seed=0, shuffle=False, train_proportion=1.0)

    # Unique id to ensure there is no race condition for the folder creation
    uuid_str = f"_{uuid.uuid4()}" if args.uuid else ""
    if args.seed < 0:
        # Seed but with a random one
        args.seed = np.random.randint(2 ** 32 - 1, dtype="int64").item()

    set_random_seed(args.seed)

    # Setting num threads to 1 makes things run faster on cpu
    if args.num_threads > 0:
        if args.verbose > 1:
            print(f"Setting torch.num_threads to {args.num_threads}")
        th.set_num_threads(args.num_threads)

    if args.trained_agent != "":
        assert args.trained_agent.endswith(".zip") and os.path.isfile(
            args.trained_agent
        ), "The trained_agent must be a valid path to a .zip file"

    tensorboard_log = None if args.tensorboard_log == "" else os.path.join(args.tensorboard_log, env_id)

    is_atari = False
    if "NoFrameskip" in env_id:
        is_atari = True

    print("=" * 10, env_id, "=" * 10)
    print(f"Seed: {args.seed}")

    # Load hyperparameters from yaml file
    with open(f"hyperparams/{args.algo}.yml", "r") as f:
        hyperparams_dict = yaml.safe_load(f)
        if args.hyperparameters in list(hyperparams_dict.keys()):
            hyperparams = hyperparams_dict[args.hyperparameters]
        elif is_atari:
            hyperparams = hyperparams_dict["atari"]
        else:
            raise ValueError(f"Hyperparameters not found for {args.algo}-{args.hyperparameters}")

    if args.hyperparams is not None:
        # Overwrite hyperparams if needed
        hyperparams.update(args.hyperparams)
    # Sort hyperparams that will be saved
    saved_hyperparams = OrderedDict([(key, hyperparams[key]) for key in sorted(hyperparams.keys())])
    # env_kwargs = {} if args.env_kwargs is None else args.env_kwargs

    algo_ = args.algo
    # HER is only a wrapper around an algo
    if args.algo == "her":
        algo_ = saved_hyperparams["model_class"]
        assert algo_ in {"sac", "ddpg", "dqn", "td3", "tqc"}, f"{algo_} is not compatible with HER"
        # Retrieve the model class
        hyperparams["model_class"] = ALGOS[saved_hyperparams["model_class"]]

    if args.verbose > 0:
        pprint(saved_hyperparams)

    n_envs = hyperparams.get("n_envs", 1)

    if args.verbose > 0:
        print(f"Using {n_envs} environments")

    # args.watch_train = False
    # args.watch_eval = False
    single_idx = args.single_idx
    env_kwargs = {}
    for i in range(n_envs):
        env_kwargs[i] = {
            "xml": train_files[single_idx],
            "param": train_params[single_idx],
            "powercoeffs": [args.powercoeff[0], args.powercoeff[1], args.powercoeff[2]],
            "render": args.watch_train and i==0,
            "is_eval": False,
        }
    eval_env_kwargs = [{
        "xml": train_files[single_idx],
        "param": train_params[single_idx],
        "powercoeffs": [args.powercoeff[0], args.powercoeff[1], args.powercoeff[2]],
        "render": args.watch_eval,
        "is_eval": True,
    }]

    # Create schedules
    for key in ["learning_rate", "clip_range", "clip_range_vf"]:
        if key not in hyperparams:
            continue
        if isinstance(hyperparams[key], str):
            schedule, initial_value = hyperparams[key].split("_")
            initial_value = float(initial_value)
            hyperparams[key] = linear_schedule(initial_value)
        elif isinstance(hyperparams[key], (float, int)):
            # Negative value: ignore (ex: for clipping)
            if hyperparams[key] < 0:
                continue
            hyperparams[key] = constant_fn(float(hyperparams[key]))
        else:
            raise ValueError(f"Invalid value for {key}: {hyperparams[key]}")

    # Should we overwrite the number of timesteps?
    if args.n_timesteps > 0:
        if args.verbose:
            print(f"Overwriting n_timesteps with n={args.n_timesteps}")
        n_timesteps = args.n_timesteps
    else:
        n_timesteps = int(hyperparams["n_timesteps"])

    normalize = False
    normalize_kwargs = {}
    if "normalize" in hyperparams.keys():
        normalize = hyperparams["normalize"]
        if isinstance(normalize, str):
            normalize_kwargs = eval(normalize)
            normalize = True
        if "gamma" in hyperparams:
            normalize_kwargs["gamma"] = hyperparams["gamma"]
        del hyperparams["normalize"]

    if "policy_kwargs" in hyperparams.keys():
        # Convert to python object if needed
        if isinstance(hyperparams["policy_kwargs"], str):
            hyperparams["policy_kwargs"] = eval(hyperparams["policy_kwargs"])

    # Delete keys so the dict can be pass to the model constructor
    if "n_envs" in hyperparams.keys():
        del hyperparams["n_envs"]
    del hyperparams["n_timesteps"]

    # obtain a class object from a wrapper name string in hyperparams
    # and delete the entry
    env_wrapper = get_wrapper_class(hyperparams)
    if "env_wrapper" in hyperparams.keys():
        del hyperparams["env_wrapper"]

    log_path = f"{args.log_folder}/{args.algo}/"
    save_path = os.path.join(log_path, f"{env_id}_{get_latest_run_id(log_path, env_id) + 1}{uuid_str}")
    params_path = f"{save_path}/{env_id}"
    os.makedirs(params_path, exist_ok=True)

    callbacks = get_callback_class(hyperparams)
    if "callback" in hyperparams.keys():
        del hyperparams["callback"]

    if args.save_freq > 0:
        # Account for the number of parallel environments
        args.save_freq = max(args.save_freq // n_envs, 1)
        callbacks.append(CheckpointCallback(save_freq=args.save_freq, save_path=save_path, name_prefix="rl_model", verbose=1))

    def create_env(n_envs, eval_env=False, no_log=False):
        """
        Create the environment and wrap it if necessary
        :param n_envs: (int)
        :param eval_env: (bool) Whether is it an environment used for evaluation or not
        :param no_log: (bool) Do not log training when doing hyperparameter optim
            (issue with writing the same file)
        :return: (Union[gym.Env, VecEnv])
        """
        global hyperparams
        global env_kwargs, eval_env_kwargs

        if eval_env:
            kwargs = eval_env_kwargs
        else:
            kwargs = env_kwargs

        # Do not log eval env (issue with writing the same file)
        log_dir = None if eval_env or no_log else save_path

        if n_envs == 1:
            # use rank=127 so eval_env won't overlap with any training_env.
            env = DummyVecEnv(
                [make_env(env_id, 127, args.seed, wrapper_class=env_wrapper, log_dir=log_dir, env_kwargs=kwargs[0])]
            )
        else:
            # env = SubprocVecEnv([make_env(env_id, i, args.seed) for i in range(n_envs)])
            # On most env, SubprocVecEnv does not help and is quite memory hungry
            env = DummyVecEnv(
                [
                    make_env(env_id, i, args.seed, log_dir=log_dir, env_kwargs=kwargs[i], wrapper_class=env_wrapper)
                    for i in range(n_envs)
                ]
            )

        # Pretrained model, load normalization
        path_ = os.path.join(os.path.dirname(args.trained_agent), args.env)
        path_ = os.path.join(path_, "vecnormalize.pkl")
        if os.path.exists(path_):
            print("Loading saved VecNormalize stats")
            env = VecNormalize.load(path_, env)
            # Deactivate training and reward normalization
            if eval_env:
                env.training = False
                env.norm_reward = False

        elif normalize:
            # Copy to avoid changing default values by reference
            local_normalize_kwargs = normalize_kwargs.copy()
            # Do not normalize reward for env used for evaluation
            if eval_env:
                if len(local_normalize_kwargs) > 0:
                    local_normalize_kwargs["norm_reward"] = False
                else:
                    local_normalize_kwargs = {"norm_reward": False}

            if args.verbose > 0:
                if len(local_normalize_kwargs) > 0:
                    print(f"Normalization activated: {local_normalize_kwargs}")
                else:
                    print("Normalizing input and reward")
            env = VecNormalize(env, **local_normalize_kwargs)

        # Optional Frame-stacking
        if hyperparams.get("frame_stack", False):
            n_stack = hyperparams["frame_stack"]
            env = VecFrameStack(env, n_stack)
            print(f"Stacking {n_stack} frames")

        if is_image_space(env.observation_space):
            if args.verbose > 0:
                print("Wrapping into a VecTransposeImage")
            env = VecTransposeImage(env)

        # check if wrapper for dict support is needed
        if args.algo == "her":
            if args.verbose > 0:
                print("Wrapping into a ObsDictWrapper")
            env = ObsDictWrapper(env)

        return env

    env = create_env(n_envs)

    # Create test env if needed, do not normalize reward
    eval_callback = None
    if args.eval_freq > 0 and not args.optimize_hyperparameters:
        # Account for the number of parallel environments
        args.eval_freq = max(args.eval_freq // n_envs, 1)

        if args.verbose > 0:
            print("Creating test environment")

        save_vec_normalize = SaveVecNormalizeCallback(save_freq=1, save_path=params_path)
        eval_callback = EvalCallback(
            create_env(1, eval_env=True),
            callback_on_new_best=save_vec_normalize,
            best_model_save_path=save_path,
            n_eval_episodes=args.eval_episodes,
            log_path=save_path,
            eval_freq=args.eval_freq,
            deterministic=not is_atari,
        )

        callbacks.append(eval_callback)

    # TODO: check for hyperparameters optimization
    # TODO: check What happens with the eval env when using frame stack
    if "frame_stack" in hyperparams:
        del hyperparams["frame_stack"]

    # Stop env processes to free memory
    if args.optimize_hyperparameters and n_envs > 1:
        env.close()

    # Parse noise string for DDPG and SAC
    if algo_ in ["ddpg", "sac", "td3"] and hyperparams.get("noise_type") is not None:
        noise_type = hyperparams["noise_type"].strip()
        noise_std = hyperparams["noise_std"]
        n_actions = env.action_space.shape[0]
        if "normal" in noise_type:
            if "lin" in noise_type:
                final_sigma = hyperparams.get("noise_std_final", 0.0) * np.ones(n_actions)
                hyperparams["action_noise"] = LinearNormalActionNoise(
                    mean=np.zeros(n_actions),
                    sigma=noise_std * np.ones(n_actions),
                    final_sigma=final_sigma,
                    max_steps=n_timesteps,
                )
            else:
                hyperparams["action_noise"] = NormalActionNoise(mean=np.zeros(n_actions), sigma=noise_std * np.ones(n_actions))
        elif "ornstein-uhlenbeck" in noise_type:
            hyperparams["action_noise"] = OrnsteinUhlenbeckActionNoise(
                mean=np.zeros(n_actions), sigma=noise_std * np.ones(n_actions)
            )
        else:
            raise RuntimeError(f'Unknown noise type "{noise_type}"')
        print(f"Applying {noise_type} noise with std {noise_std}")
        del hyperparams["noise_type"]
        del hyperparams["noise_std"]
        if "noise_std_final" in hyperparams:
            del hyperparams["noise_std_final"]

    if args.trained_agent.endswith(".zip") and os.path.isfile(args.trained_agent):
        # Continue training
        print("Loading pretrained agent")
        # Policy should not be changed
        del hyperparams["policy"]

        if "policy_kwargs" in hyperparams.keys():
            del hyperparams["policy_kwargs"]

        model = ALGOS[args.algo].load(
            args.trained_agent, env=env, seed=args.seed, tensorboard_log=tensorboard_log, verbose=args.verbose, **hyperparams
        )

        replay_buffer_path = os.path.join(os.path.dirname(args.trained_agent), "replay_buffer.pkl")
        if os.path.exists(replay_buffer_path):
            print("Loading replay buffer")
            if args.algo == "her":
                # if we use HER we have to add an additional argument
                model.load_replay_buffer(replay_buffer_path, args.truncate_last_trajectory)
            else:
                model.load_replay_buffer(replay_buffer_path)

    elif args.optimize_hyperparameters:

        if args.verbose > 0:
            print("Optimizing hyperparameters")

        if args.storage is not None and args.study_name is None:
            warnings.warn(
                f"You passed a remote storage: {args.storage} but no `--study-name`."
                "The study name will be generated by Optuna, make sure to re-use the same study name "
                "when you want to do distributed hyperparameter optimization."
            )

        def create_model(*_args, **kwargs):
            """
            Helper to create a model with different hyperparameters
            """
            return ALGOS[args.algo](env=create_env(n_envs, no_log=True), tensorboard_log=tensorboard_log, verbose=0, **kwargs)

        data_frame = hyperparam_optimization(
            args.algo,
            create_model,
            create_env,
            n_trials=args.n_trials,
            n_timesteps=n_timesteps,
            hyperparams=hyperparams,
            n_jobs=args.n_jobs,
            seed=args.seed,
            sampler_method=args.sampler,
            pruner_method=args.pruner,
            n_startup_trials=args.n_startup_trials,
            n_evaluations=args.n_evaluations,
            storage=args.storage,
            study_name=args.study_name,
            verbose=args.verbose,
            deterministic_eval=not is_atari,
        )

        report_name = (
            f"report_{env_id}_{args.n_trials}-trials-{n_timesteps}" f"-{args.sampler}-{args.pruner}_{int(time.time())}.csv"
        )

        log_path = os.path.join(args.log_folder, args.algo, report_name)

        if args.verbose:
            print(f"Writing report to {log_path}")

        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        data_frame.to_csv(log_path)
        exit()
    else:
        # Train an agent from scratch
        model = ALGOS[args.algo](env=env, tensorboard_log=tensorboard_log, seed=args.seed, verbose=args.verbose, **hyperparams)

    kwargs = {}
    if args.log_interval > -1:
        kwargs = {"log_interval": args.log_interval}

    if len(callbacks) > 0:
        kwargs["callback"] = callbacks

    # Save hyperparams
    with open(os.path.join(params_path, "config.yml"), "w") as f:
        yaml.dump(saved_hyperparams, f)

    # save command line arguments
    with open(os.path.join(params_path, "args.yml"), "w") as f:
        ordered_args = OrderedDict([(key, vars(args)[key]) for key in sorted(vars(args).keys())])
        yaml.dump(ordered_args, f)

    print(f"Log path: {save_path}")

    try:
        model.learn(n_timesteps, **kwargs)
    except KeyboardInterrupt:
        pass
    finally:
        # Release resources
        env.close()

    # Save trained model

    print(f"Saving to {save_path}")
    model.save(f"{save_path}/{env_id}")

    if hasattr(model, "save_replay_buffer") and args.save_replay_buffer:
        print("Saving replay buffer")
        model.save_replay_buffer(os.path.join(save_path, "replay_buffer.pkl"))

    if normalize:
        # Important: save the running average, for testing the agent we need that normalization
        model.get_vec_normalize_env().save(os.path.join(params_path, "vecnormalize.pkl"))
        # Deprecated saving:
        # env.save_running_average(params_path)
