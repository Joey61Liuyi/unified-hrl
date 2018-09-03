import numpy as np
from image_processing import *
import random
import torch
import os
from model import DQN
import torch
from torch.autograd import Variable
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import torchvision.transforms as T

from logger import Logger
logger = Logger('./logs')

# Global Variables
BATCH_SIZE = 32
TASK = 'MontezumaRevenge-v0'
REPLAY_BUFFER_SIZE = 1000000
FRAME_HISTORY_LEN = 4
TARGET_UPDATE_FREQ = 10000
GAMMA = 0.99
LEARNING_FREQ = 4
LEARNING_RATE = 0.00025
ALPHA = 0.95
EPS = 0.01
LEARNING_STARTS = 1000
MAX_FRAMES = 10000000
gamma = 0.99
num_param_updates = 0
mean_episode_reward      = -float('nan')
best_mean_episode_reward = -float('inf')
LOG_EVERY_N_STEPS = 100
SAVE_MODEL_EVERY_N_STEPS = 100000
SUBGOAL_DISCOVERY_FREQ = REPLAY_BUFFER_SIZE // 10

def sample_from_random_subgoal_set(subgoal_set):
	index = random.randint(0, len(subgoal_set)-1)
	return index, subgoal_set[index]
# Create random subgoals from image features 
rec = Recognizer()
base_img = cv2.imread('./templates/base.png')
img_edge = base_img[55:181,10:150,:]
im2, contours, hierarchy = edge_detector(img_edge)
coords = rec.get(base_img)
random_subgoals_set = create_random_subgoal_set_from_objects(coords)
# This is how we find if the subgoal is attained
man_mask = rec.get_man_mask(base_img)
subgoal_id, random_subgoal_mask = sample_from_random_subgoal_set(random_subgoals_set)
random_subgoal_frame = create_mask_frame(base_img,random_subgoal_mask)
intrinsic_learning_task_done = are_masks_align(man_mask, random_subgoal_mask)

# BUILD MODEL 
USE_CUDA = torch.cuda.is_available()
if torch.cuda.is_available():
	device0 = torch.device("cuda:0")
else:
	device0 = torch.device("cpu")

dtype = torch.cuda.FloatTensor if torch.cuda.is_available() else torch.FloatTensor
dlongtype = torch.cuda.LongTensor if torch.cuda.is_available() else torch.LongTensor
duinttype = torch.cuda.ByteTensor if torch.cuda.is_available() else torch.ByteTensor

Qt = DQN(in_channels=5, num_actions=18).type(dtype)
Qt_t = DQN(in_channels=5, num_actions=18).type(dtype)
Qt_t.load_state_dict(Qt.state_dict())
Qt_t.eval()
for param in Qt_t.parameters():
	param.requires_grad = False

if torch.cuda.device_count() > 0:
	Qt.cuda()
	Qt = nn.DataParallel(Qt).to(device0)
	Qt_t = nn.DataParallel(Qt_t).to(device0)
	batch_size = BATCH_SIZE * torch.cuda.device_count()
else:
	batch_size = BATCH_SIZE

# optimizer
ALPHA = 0.95
EPS = 0.01
LEARNING_RATE = 0.00025
optimizer = optim.RMSprop(Qt.parameters(),lr=LEARNING_RATE, alpha=ALPHA, eps=EPS)

# training parameters
action_repeat = 4
batch_size = 32
memory_size = 10000
controller_update_freq = 4
meta_contoller_update_freq = 1000
noop_max = 30 # no operation at the beggining of the game
skip_frame = 4
max_frames = 50000000
max_play_random = 50000
max_steps = 1000
alpha = 0.00025
# Create environment
import gym
env = gym.make('MontezumaRevenge-v0')
from gym import wrappers

actions_meaning = env.unwrapped.get_action_meanings()

# from hrl import MetaLearning
# agent = MetaLearning()

def reset(noop_max=30):	
	s = env.reset()
	noop_random = random.randint(1, noop_max)
	for _ in range(noop_random):
		s,_,_,_ = env.step(0)
	S,_, _, _ = step(0)
	return S

def skip_frames(a,skip_frame=4):	
	frames = []
	rewards = []
	for _ in range(skip_frame):
		s,r,done,step_info = env.step(a)
		frames.append(s)
		rewards.append(r)
	s_max = np.max(np.stack(frames), axis=0)
	total_rewards = sum(rewards)
	return s_max, total_rewards, done, step_info 

def step(a,repeat_action=4):
	frames = []
	rewards = []
	for _ in range(repeat_action):
		s,r,done,step_info = skip_frames(a)
		frames.append(s)
		rewards.append(r)
	total_rewards = sum(rewards)
	return frames, total_rewards, done, step_info 

def get_man_mask(frames):
	s_max = np.max(np.stack(frames), axis=0)
	man_mask = rec.get_man_mask(s_max)
	return man_mask

def get_man_xy_np_coordinate(man_mask):
	x = man_mask.x
	y = man_mask.y
	return np.array([x,y])

def four_frames_to_4_84_84(S):
	""" 0) Atari frames: 210 x 160
		1) Get image grayscale
		2) Rescale image 110 x 84
		3) Crop center 84 x 84 (you can crop top/bottom according to the game)
		"""
	for i, img in enumerate(S):
		gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
		h, w = 110, 84
		gray_resized = cv2.resize(gray,(w,h))
		gray_cropped = gray_resized[13:110-13,:]
		gray_reshaped = gray_cropped.reshape((1,84,84))
		if i == 0:
			s = gray_reshaped
		else:
			s = np.concatenate((s,gray_reshaped),axis=0)
	return s

def single_channel_frame_to_1_84_84(subgoal_frame):
	reshaped = subgoal_frame.reshape((210,160))
	resized =  cv2.resize(reshaped,(84,110))
	cropped = resized[13:110-13,:]
	g = cropped.reshape((1,84,84))
	return g

def epsilon_greedy(Q,epsilon=0.1):
	if random.random() < epsilon:
		return env.action_space.sample()
	else:
		return Q.argmax()

# create an Expereince Memory
from memory import Experience, ExperienceMemory
experience_memory = ExperienceMemory(size=memory_size)

from subgoal_discovery import SubgoalDiscovery
sd = SubgoalDiscovery()

# centroid_low_return = []
# centroid_high_return = []

# outliers_low_return = []
# outliers_high_return = []

outliers = []
centroids = []
G = []
episode_rewards = []

### PHASE I: INTRINSIC MOTIVATION LEARNING + SUBGOAL DISCOVERY 
### Goal: Training the Controller via intrinsic motivation learning
### Random subgoals and explorations
# just Controller, not Meta-Controller
S = reset()
s = four_frames_to_4_84_84(S)
man_mask = get_man_mask(S)
man_loc = get_man_xy_np_coordinate(man_mask)
subgoal_index, subgoal_mask = sample_from_random_subgoal_set(random_subgoals_set) # random g
subgoal_frame = create_mask_frame(base_img,subgoal_mask)
g = single_channel_frame_to_1_84_84(subgoal_frame)
steps = 0
R = 0 # return
firs_time = True
num_param_updates = 0
epsilon = 1
first_time_kmeans = True
for t in range(max_frames):
	x = np.concatenate((s,g),axis=0).reshape((1,5,84,84))
	if t < LEARNING_STARTS:
		a = env.action_space.sample()
	else:
		qt = Qt.forward(torch.Tensor(x).type(dtype)/255)
		a = epsilon_greedy(qt.cpu().detach().numpy(),epsilon=epsilon) # random action
	SP, r, terminal, step_info = step(a)
	episode_rewards.append(r)
	sp = four_frames_to_4_84_84(SP)
	xp = np.concatenate((sp,g),axis=0).reshape((1,5,84,84))	
	man_mask = get_man_mask(SP)
	man_loc = get_man_xy_np_coordinate(man_mask)
	intrinsic_done_task = are_masks_align(man_mask, subgoal_mask)
	# outlier 
	if r > 0:
		outliers.append(man_loc)
	R += r
	
	r = np.clip(r, -1.0, 1.0)	
	
	g_id = subgoal_index
		
	if intrinsic_done_task:
		intrinsic_done = 1
		tilde_r = +1
		subgoal_index, subgoal_mask = sample_from_random_subgoal_set(random_subgoals_set)
		subgoal_frame = create_mask_frame(base_img,subgoal_mask)
	else:
		intrinsic_done = 0
		tilde_r = -0.1

	if terminal and env.unwrapped.ale.lives() > 0:
		done = 1
	else:
		done = 0

	experience = Experience(s, g, g_id, a, r, sp, intrinsic_done, done, man_loc)
	experience_memory.push(experience)

	epsilon = 1 - (1 - 0.1) * t / 1000000 
	s = sp
	steps += 1

	if terminal or (steps>max_steps):	
		S = reset() # s is reserved for 4*84*84 input image
		s = four_frames_to_4_84_84(S)
		man_mask = get_man_mask(S)
		man_loc = get_man_xy_np_coordinate(man_mask)
		subgoal_index, subgoal_mask = sample_from_random_subgoal_set(random_subgoals_set) # random g
		subgoal_frame = create_mask_frame(base_img,subgoal_mask)
		g = single_channel_frame_to_1_84_84(subgoal_frame)
		steps = 0
		R = 0
		episode_rewards = []

	if (t > LEARNING_STARTS and t % LEARNING_FREQ == 0):
		states, subgoals, actions, rewards, state_primes, intrinsic_dones = \
						experience_memory.sample_controller(batch_size=batch_size)
		x = np.concatenate((states,subgoals),axis=1)	
		xp = np.concatenate((state_primes,subgoals),axis=1)

		
		if torch.cuda.is_available():
			with torch.cuda.device(0):
				x = torch.Tensor(x).to(device0).type(dtype)/255
				xp = torch.Tensor(xp).to(device0).type(dtype)/255
				actions = torch.Tensor(actions).to(device0).type(dlongtype)
				rewards = torch.Tensor(rewards).to(device0).type(dtype)
				intrinsic_dones = torch.Tensor(intrinsic_dones).type(duinttype)
		else:
			x = torch.Tensor(x).type(dtype) / 255
			xp = torch.Tensor(x).type(dtype)/255
			actions = torch.Tensor(actions).type(dlongtype)
			rewards = torch.Tensor(rewards).type(dtype)
			intrinsic_dones = torch.type(duinttype)
	


		if torch.cuda.device_count() > 0:
			Qt.to(device0)
			Qt_t = Qt_t.to(device0)

		qt_values = Qt.forward(x)
		qt = qt_values.gather(1, actions.unsqueeze(1))
		qt = qt.squeeze()
		
		qt_p1 = Qt.forward(xp)
		_, a_prime = qt_p1.max(1)

		qt_t_p1 = Qt_t.forward(xp).detach()
		qt_t_prime = qt_t_p1.gather(1, a_prime.unsqueeze(1))
		qt_t_prime = qt_t_prime.squeeze()

		qt_t = (1 - intrinsic_dones) * qt_t_prime
		error = rewards + gamma * qt_t - qt
		clipped_error = -1.0 * error.clamp(-1, 1)
		optimizer.step()
		num_param_updates += 1
		if num_param_updates % TARGET_UPDATE_FREQ == 0:
			Qt_t.load_state_dict(Qt.state_dict())

		if t % LOG_EVERY_N_STEPS == 0:
			for tag, value in Qt.named_parameters():
				tag = tag.replace('.', '/')
				logger.histo_summary(tag, value.data.numpy(), t+1)
				if value.grad is not None:
					logger.histo_summary(tag+'/grad', value.grad.numpy(), t+1)

	if t % SAVE_MODEL_EVERY_N_STEPS == 0:
		if not os.path.exists("models"):
			os.makedirs("models")
		add_str = 'DQN'
		model_save_path = "models/%s_%s_%d_%s.model" %(str(TASK), add_str, t, str(time.ctime()).replace(' ', '_'))
		torch.save(Qt.state_dict(), model_save_path)

	if len(episode_rewards) > 0:
		mean_episode_reward = np.mean(episode_rewards[-100:])
		best_mean_episode_reward = max(best_mean_episode_reward, mean_episode_reward)

	if t % LOG_EVERY_N_STEPS == 0:
		print("---------------------------------")
		print("Timestep %d" % (t,))
		print("learning started? %d" % (t > LEARNING_STARTS))
		print("mean reward (100 episodes) %f" % mean_episode_reward)
		print("best mean reward %f" % best_mean_episode_reward)
		print("episodes %d" % len(episode_rewards))
		print("exploration %f" % epsilon)
		print("learning_rate %f" % LEARNING_RATE)
		sys.stdout.flush()

		#============ TensorBoard logging ============#
		# Log the scalar values
		info = {
			'learning_started': (t > LEARNING_STARTS),
			'num_episodes': len(episode_rewards),
			'exploration': epsilon,
			'learning_rate': LEARNING_RATE,
		}

		for tag, value in info.items():
			logger.scalar_summary(tag, value, t+1)

		if len(episode_rewards) > 0:
			info = {
				'last_episode_rewards': episode_rewards[-1],
			}

			for tag, value in info.items():
				logger.scalar_summary(tag, value, t+1)

		if (best_mean_episode_reward != -float('inf')):
			info = {
				'mean_episode_reward_last_100': mean_episode_reward,
				'best_mean_episode_reward': best_mean_episode_reward
			}

			for tag, value in info.items():
				logger.scalar_summary(tag, value, t+1)
	if t % SUBGOAL_DISCOVERY_FREQ == 0 and t > 0:
		if first_time_kmeans:
			X = experience_memory.get_man_positions()
			sd.feed_data(X)
			C = sd.find_kmeans_clusters()
			print('current discovered centroids: ', C)
			print(C)
			first_time_kmeans = False
		else:
			X = experience_memory.get_man_positions()
			sd.feed_data(X)
			C = sd.find_kmeans_clusters(init=C)
			print('current discovered centroids: ', C)







