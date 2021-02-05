import numpy as np
import tensorflow as tf
import sys
import argparse
import random
import scipy
import sklearn
from sklearn.manifold import TSNE
import matplotlib.pyplot as plt
import seaborn as sns
import uuid
import json
import os
import config

HSMM_ROOT = 'C:/Users/dylan/Documents/seg/hsmm/'

class Sampling(tf.keras.layers.Layer):
    def call(self, inputs):
        z_mean, z_log_var = inputs
        batch = tf.shape(z_mean)[0]
        dim = tf.shape(z_mean)[1]
        epsilon = tf.random.normal(shape=(batch, dim))
        return z_mean + tf.math.exp(.5 * z_log_var) * epsilon

class VAE(tf.keras.models.Model):
    def __init__(self, seq_len, input_dim, hidden_dim, beta, warm_up_iters):
        super(VAE, self).__init__()
        self.hidden_dim = hidden_dim
        self.beta = tf.cast(beta, tf.float32)
        self.warm_up_iters = tf.cast(warm_up_iters, tf.float32)
        self.encoder = tf.keras.models.Sequential([
            tf.keras.layers.Input(shape=(seq_len, input_dim)),
            tf.keras.layers.LSTM(hidden_dim),
            tf.keras.layers.Dropout(.5),
            tf.keras.layers.Dense(hidden_dim * 2)
        ])
        self.decoder = tf.keras.models.Sequential([
            tf.keras.layers.Input(shape=(hidden_dim,)),
            tf.keras.layers.RepeatVector(seq_len),
            tf.keras.layers.LSTM(hidden_dim, return_sequences=True),
            tf.keras.layers.TimeDistributed(tf.keras.layers.Dense(input_dim))
        ])
        self.train_loss = tf.keras.metrics.Mean(name='train_loss')
        self.reconstr_loss_train = tf.keras.metrics.Mean(name='reconstr_loss_train')
        self.kl_loss_train = tf.keras.metrics.Mean(name='kl_loss_train')
        self.dev_loss = tf.keras.metrics.Mean(name='dev_loss')

    def call(self, x):
        z_mean, z_log_var = self.encode(x)
        z = self.reparameterize(z_mean, z_log_var)
        return self.decoder(z)

    def encode(self, x):
        z_mean, z_log_var = tf.split(self.encoder(x), num_or_size_splits=2, axis=1)
        return z_mean, z_log_var

    def reparameterize(self, z_mean, z_log_var):
        batch = tf.shape(z_mean)[0]
        dim = tf.shape(z_mean)[1]
        eps = tf.random.normal(shape=(batch, dim))
        return eps * tf.exp(z_log_var * .5) + z_mean

    @tf.function
    def train_step(self, x):
        beta = self.beta * tf.math.minimum(tf.cast(self.optimizer.iterations, tf.float32) / self.warm_up_iters, tf.cast(1., tf.float32))
        with tf.GradientTape() as tape:
            z_mean, z_log_var = self.encode(x)
            z = self.reparameterize(z_mean, z_log_var)
            x_reconstr = self.decoder(z)
            x_masked = tf.boolean_mask(x, tf.not_equal(x, -1e9))
            x_reconstr_masked = tf.boolean_mask(x_reconstr, tf.not_equal(x, -1e9))
            reconstr_loss = tf.math.reduce_mean(tf.keras.losses.mse(x_masked, x_reconstr_masked))
            kl_loss = -.5 * (1 + z_log_var - tf.math.square(z_mean) - tf.math.exp(z_log_var))
            kl_loss = tf.math.reduce_mean(tf.math.reduce_sum(kl_loss, axis=1))
            total_loss = reconstr_loss + beta * kl_loss
        gradients = tape.gradient(total_loss, self.trainable_variables)
        self.optimizer.apply_gradients(zip(gradients, self.trainable_variables))
        self.train_loss(total_loss)
        self.reconstr_loss_train(reconstr_loss)
        self.kl_loss_train(kl_loss)
        return {
            'loss': self.train_loss.result(),
            'reconstr_loss': self.reconstr_loss_train.result(),
            'kl_loss': self.kl_loss_train.result(),
            'beta': beta
        }

    @tf.function
    def test_step(self, x):
        z_mean, z_log_var = self.encode(x)
        x_reconstr = self.decoder(z_mean)
        x_masked = tf.boolean_mask(x, tf.not_equal(x, -1e9))
        x_reconstr_masked = tf.boolean_mask(x_reconstr, tf.not_equal(x, -1e9))
        reconstr_loss = tf.math.reduce_mean(tf.keras.losses.mse(x_masked, x_reconstr_masked))
        kl_loss = -.5 * (1 + z_log_var - tf.math.square(z_mean) - tf.math.exp(z_log_var))
        kl_loss = tf.math.reduce_mean(tf.math.reduce_sum(kl_loss, axis=1))
        total_loss = reconstr_loss + self.beta * kl_loss
        self.dev_loss(total_loss)
        return {
            'loss': self.dev_loss.result(),
        }

class AutoencoderWrapper:
    @classmethod
    def add_args(cls, parser):
        parser.add_argument('--vae_hidden_size', type=int, default=8)
        parser.add_argument('--vae_batch_size', type=int, default=10)
        parser.add_argument('--vae_beta', type=int, default=10)
        parser.add_argument('--vae_warm_up_iters', type=int, default=1000)

    def __init__(self, args, nbc_wrapper):
        self.args = args
        self.nbc_wrapper = nbc_wrapper
        self.x, self.y = self.nbc_wrapper.x, self.nbc_wrapper.y
        self.make_paths()
        self.get_autoencoder()

    def make_paths(self):
        for dir in ['weights', 'encodings', 'reconstructions']:
            dirpath = '{}autoencoder/{}'.format(HSMM_ROOT, dir)
            if not os.path.exists(dirpath):
                os.makedirs(dirpath)

    def try_load_model(self):
        args_id = config.args_to_id(self.args, model='autoencoder')
        keypath = HSMM_ROOT + 'autoencoder/keys.json'
        if not os.path.exists(keypath):
            return False
        with open(keypath) as f:
            keys = json.load(f)
        if args_id not in keys:
            return False
        fid = keys[args_id]
        weights_path = HSMM_ROOT + 'autoencoder/weights/{}.h5'.format(fid)
        encodings_path = HSMM_ROOT + 'autoencoder/encodings/{}.json'.format(fid)
        reconstructions_path = HSMM_ROOT + 'autoencoder/reconstructions/{}.json'.format(fid)
        self.vae(self.x['train'])
        self.vae.load_weights(weights_path)
        with open(encodings_path) as f:
            self.encodings = json.load(f)
        with open(reconstructions_path) as f:
            self.reconstructions = json.load(f)
        for type in ['train', 'dev', 'test']:
            self.encodings[type] = np.array(self.encodings[type])
            self.reconstructions[type] = np.array(self.reconstructions[type])
        return True

    def save_model(self):
        args_id = config.args_to_id(self.args, model='autoencoder')
        keypath = HSMM_ROOT + 'autoencoder/keys.json'
        if os.path.exists(keypath):
            with open(keypath) as f:
                keys = json.load(f)
        else:
            keys = {}
        fid = str(uuid.uuid1())
        weights_path = HSMM_ROOT + 'autoencoder/weights/{}.h5'.format(fid)
        encodings_path = HSMM_ROOT + 'autoencoder/encodings/{}.json'.format(fid)
        reconstructions_path = HSMM_ROOT + 'autoencoder/reconstructions/{}.json'.format(fid)
        self.vae.save_weights(weights_path)
        with open(encodings_path, 'w+') as f:
            serialized = {}
            for type in ['train', 'dev', 'test']:
                serialized[type] = self.encodings[type].tolist()
            json.dump(serialized, f)
        with open(reconstructions_path, 'w+') as f:
            serialized = {}
            for type in ['train', 'dev', 'test']:
                serialized[type] = self.reconstructions[type].tolist()
            json.dump(serialized, f)
        keys[args_id] = fid
        with open(keypath, 'w+') as f:
            json.dump(keys, f)
        print('saved autoencoder')

    def train_autoencoder(self):
        _, seq_len, input_dim = self.x['train'].shape
        train_dset = tf.data.Dataset.from_tensor_slices(self.x['train']).batch(self.args.vae_batch_size)
        dev_dset = tf.data.Dataset.from_tensor_slices(self.x['dev']).batch(self.args.vae_batch_size)

        negative_log_likelihood = lambda x, rv_x: -rv_x.log_prob(x)
        self.vae.compile(optimizer='adam', loss=negative_log_likelihood)
        callbacks = [
            tf.keras.callbacks.EarlyStopping(patience=10, verbose=1),
            tf.keras.callbacks.ModelCheckpoint(HSMM_ROOT + 'autoencoder/weights/tmp.h5', save_best_only=True, verbose=1)
        ]
        self.vae.fit(x=train_dset, epochs=1000, shuffle=True, validation_data=dev_dset, callbacks=callbacks, verbose=1)
        self.vae(self.x['train'])
        self.vae.load_weights(HSMM_ROOT + 'autoencoder/weights/tmp.h5')
        self.encodings = {}
        self.reconstructions = {}
        for type in ['train', 'dev', 'test']:
            z, _ = self.vae.encode(self.x[type])
            x_ = self.vae(self.x[type])
            self.encodings[type] = z.numpy()
            self.reconstructions[type] = x_.numpy()

    def get_autoencoder(self):
        _, seq_len, input_dim = self.x['train'].shape
        self.vae = VAE(seq_len, input_dim, self.args.vae_hidden_size, self.args.vae_beta, self.args.vae_warm_up_iters)
        if self.try_load_model():
            print('loaded saved model')
            return
        self.train_autoencoder()
        self.save_model()
