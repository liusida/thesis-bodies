import arguments
import matplotlib.pyplot as plt
import numpy as np
from utils import ALGOS

# it seems the mal info doesn't surpressed by the first layer. strange!

if __name__ == "__main__":
    args = arguments.get_dissect_arguments()
    algo = "ppo"
    model = ALGOS[algo].load(args.model_path, env=None)
    net = model.policy.mlp_extractor.policy_net
    layer_1 = net._modules['0']
    weight = layer_1.weight.detach().numpy()
    bias = layer_1.bias.detach().numpy().reshape([-1,1])
    fig, axes = plt.subplots(nrows=1, ncols=2, figsize=[16,12])
    axes = axes.flatten()
    ax = axes[0]
    ax.imshow(weight)
    ax = axes[1]
    ax.imshow(bias)
    ax.set_axis_off()
    # plt.show()

    for i in range(23):
        print(i,"====")
        obs = np.zeros([23])
        obs[i] = 100
        print(obs)
        ret = weight.dot(obs)
        # print(ret)
        print(np.max(ret))