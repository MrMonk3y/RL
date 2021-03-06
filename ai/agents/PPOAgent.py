from ai.structure.SumTree import *
import numpy as np
import tensorflow as tf
import gym
import sklearn.preprocessing
import sklearn.pipeline
from sklearn.kernel_approximation import RBFSampler
from numpy import newaxis
from sklearn.utils import shuffle

class PPOAgent:
    def __init__(self, env:gym.Env, n_actions, n_features, action_low, action_high, featurize=False, reward_decay=0.95,
                 actor_learning_rate=0.01, critic_learning_rate=0.01, learning_rate_decay=0.95,
                 ):
        self.env = env
        self.state_size = n_features
        self.action_size = n_actions
        self.action_low = action_low
        self.action_high = action_high
        self.gamma = reward_decay   # discount rate
        self.actor_model_set = True
        self.critic_model_set = True
        self.actor_learning_rate = actor_learning_rate
        self.critic_learning_rate = critic_learning_rate # often larger than actor_learning_rate
        self.learning_rate_decay = learning_rate_decay
        self.batch_size = 64
        self.epsilon = 0.2 # used to clip
        self.entfact = 1e-3 # entropy factor, to encourage exploration
        self.lam = 0.95 # gae factor
        self.memory = [] # store (s, a, r) for one agent
        self.agents = 5 # number of agents that collect memory
        self.history = {} # store the memory for different agents
        self.history['states'] = []
        self.history['actions'] = []
        self.history['discounted_rs'] = []
        self.history['advantages'] = []
        self.featurize = featurize
        if featurize:
            self._init_featurizer()
        self._construct_nets()
        
    def _construct_nets(self):
        self.sess = tf.Session()
        self.tfs = tf.placeholder(tf.float32, [None, self.state_size], 'state')

        # critic
        with tf.variable_scope('critic'):
            net = tf.layers.dense(self.tfs, 200, tf.nn.relu)
#             net = tf.layers.dense(net, 100, tf.nn.relu)
#             net = tf.layers.dense(net, 30, tf.nn.relu)
            self.v = tf.layers.dense(net, 1)
            self.tfdc_r = tf.placeholder(tf.float32, [None, 1], 'discounted_r')
            self.closs = tf.reduce_mean(tf.square(self.tfdc_r - self.v))
            self.ctrain_op = tf.train.AdamOptimizer(self.critic_learning_rate).minimize(self.closs)

        # actor
        pi, pi_params = self._build_anet('pi', trainable=True)
        oldpi, oldpi_params = self._build_anet('oldpi', trainable=False)
        with tf.variable_scope('sample_action'):
            self.sample_op = tf.squeeze(pi.sample(1), axis=0)       # choosing action
        with tf.variable_scope('update_oldpi'):
            self.update_oldpi_op = [oldp.assign(p) for p, oldp in zip(pi_params, oldpi_params)]

        self.tfa = tf.placeholder(tf.float32, [None, self.action_size], 'action')
        self.tfadv = tf.placeholder(tf.float32, [None, 1], 'advantage')
        with tf.variable_scope('loss'):
            with tf.variable_scope('surrogate'):
#                 ratio = tf.exp(pi.log_prob(self.tfa) - oldpi.log_prob(self.tfa))
                self.ratio = pi.prob(self.tfa) / (oldpi.prob(self.tfa)+1e-10)
                surr = self.ratio * self.tfadv
                surr2 = tf.clip_by_value(self.ratio, 1-self.epsilon, 1+self.epsilon) * self.tfadv
                self.aloss = - tf.reduce_mean(tf.minimum(surr, surr2)) - self.entfact * tf.reduce_mean(pi.entropy())

        with tf.variable_scope('atrain'):
            self.atrain_op = tf.train.AdamOptimizer(self.actor_learning_rate).minimize(self.aloss, var_list=pi_params)

        self.saver = tf.train.Saver()
        
        self.sess.run(tf.global_variables_initializer())
        
    def _build_anet(self, name, trainable):
        with tf.variable_scope(name):
            net = tf.layers.dense(self.tfs, 30, tf.nn.relu, trainable=trainable)
#             net = tf.layers.dense(net, 100, tf.nn.relu, trainable=trainable)
#             net = tf.layers.dense(net, 30, tf.nn.relu, trainable=trainable)
            mu = max(np.abs(self.action_low), np.abs(self.action_high)) * tf.layers.dense(net, self.action_size, tf.nn.tanh, trainable=trainable)
            sigma = tf.layers.dense(net, self.action_size, tf.nn.softplus, trainable=trainable)
            norm_dist = tf.distributions.Normal(loc=mu, scale=sigma)
        params = tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES, scope=name)
        return norm_dist, params
    
    def choose_action(self, state): # normal distribution
        assert self.actor_model_set, 'actor model not set!'
        a = self.sess.run(self.sample_op, {self.tfs: state})[0]
        return np.clip(a, self.action_low, self.action_high)
    
    def remember(self, state, action, reward, next_state):
        self.memory += [[state[0], action, reward, next_state[0]]]
    
    def discount_rewards(self, rewards, gamma, value_next=0.0):
        discounted_r = np.zeros_like(rewards)
        running_add = value_next
        for t in reversed(range(0, len(rewards))):
            discounted_r[t] = running_add = running_add * gamma + rewards[t]
        return discounted_r
    
    def process_memory(self):
        memory = np.vstack(self.memory)
        states = np.vstack(memory[:,0])
        actions = np.vstack(memory[:,1])
        rewards = memory[:,2]
        discounted_ep_rs = self.discount_rewards(rewards, self.gamma)[:, newaxis]
        value_estimates = self.sess.run(self.v, {self.tfs: states}).flatten()
        value_estimates = np.append(value_estimates, 0)
        delta_t = rewards + self.gamma * value_estimates[1:] - value_estimates[:-1]
        advs = self.discount_rewards(delta_t, self.gamma * self.lam)[:, newaxis] #gae
        self.history['states'] += [states]
        self.history['actions'] += [actions]
        self.history['discounted_rs'] += [discounted_ep_rs]
        self.history['advantages'] += [advs]
        self.memory = [] # empty the memory
    
    def replay(self):
        assert self.actor_model_set, 'model not set!'
        assert self.critic_model_set, 'critic model not set!'
        self.sess.run(self.update_oldpi_op)
        
        s = np.vstack(self.history['states'])
        ac = np.vstack(self.history['actions'])
        dc_r = np.vstack(self.history['discounted_rs'])
        ad = np.vstack(self.history['advantages'])
        
        for _ in range(10): # update K epochs
            s, ac, dc_r, ad = shuffle(s, ac, dc_r, ad)
            for l in range(s.shape[0]//self.batch_size):
                start = l * self.batch_size
                end = (l + 1) * self.batch_size
                self.sess.run(self.atrain_op, {self.tfs: s[start:end, :], self.tfa: ac[start:end, :], self.tfadv: ad[start:end, :]})
                self.sess.run(self.ctrain_op, {self.tfs: s[start:end, :], self.tfdc_r: dc_r[start:end, :]})
        self.actor_learning_rate *= self.learning_rate_decay
        self.critic_learning_rate *= self.learning_rate_decay
        
        for key in self.history:
            self.history[key] = [] # empty the history
        
    def _init_featurizer(self):
        # Feature preprocessing: Normalize to zero mean and unit variance
        # We use a few samples from the observation space to do this
        observation_examples = np.array([self.env.observation_space.sample() for _ in range(10000)])
        self.scaler = sklearn.preprocessing.StandardScaler()
        self.scaler.fit(observation_examples)
        
        # Used to converte a state to a featurized representation.
        # We use RBF kernels with different variances to cover different parts of the space
        self.featurizer = sklearn.pipeline.FeatureUnion([
                ("rbf1", RBFSampler(gamma=5.0, n_components=100)),
                ("rbf2", RBFSampler(gamma=2.0, n_components=100)),
                ("rbf3", RBFSampler(gamma=1.0, n_components=100)),
                ("rbf4", RBFSampler(gamma=0.5, n_components=100))
                ])
        self.featurizer.fit(self.scaler.transform(observation_examples))
        self.state_size = 400
        
    def featurize_state(self, state):
        scaled = self.scaler.transform(state)
        featurized = self.featurizer.transform(scaled)
        return featurized
        
    def learn(self, n_episodes, visualize=False, verbose=0):
        assert self.actor_model_set, 'actor model not set!'
        assert self.critic_model_set, 'critic model not set!'
        rewards = []
        for i_episode in range(n_episodes):
            if i_episode > 0 and i_episode % self.agents == 0: # update every self.agent episodes
                self.replay()
                
            state = self.env.reset()
            state = state[np.newaxis, :]
            if self.featurize:
                state = self.featurize_state(state)
            r = 0
            while True:
                if visualize:
                    self.env.render()
                action = self.choose_action(state)
                next_state, reward, done, _ = self.env.step(action)
                r += reward
                next_state = next_state[np.newaxis, :]
                if self.featurize:
                    next_state = self.featurize_state(next_state)
                self.remember(state, action, reward, next_state)
                state = next_state
                if done:
                    if verbose > 0:
                        print("episode:", i_episode+1, "rewards:", r)
                    self.process_memory()
                    rewards += [r]
                    break
        print("finished learning!")
        return rewards
    
    def test(self, n_episodes, visualize=True, verbose=1):
        assert self.actor_model_set, 'actor model not set!'
        assert self.critic_model_set, 'critic model not set!'
        rewards = []
        for i_episode in range(n_episodes):
            state = self.env.reset()
            state = state[np.newaxis, :]
            if self.featurize:
                state = self.featurize_state(state)
            r = 0
            while True:
                if visualize:
                    self.env.render()
                action = self.choose_action(state)
                next_state, reward, done, _ = self.env.step(action)
                r += reward
                next_state = next_state[np.newaxis, :]
                if self.featurize:
                    next_state = self.featurize_state(next_state)
                state = next_state
                if done:
                    if verbose > 0:
                        print("episode:", i_episode+1, "rewards:", r)
                    rewards += [r]
                    break
        print("finished testing!")
        return rewards