import numpy as np
import tensorflow as tf
import sys
sys.path.append('C:/Users/dylan/Documents/')
from nbc.nbc_wrapper import NBCWrapper
import argparse
import random
import scipy
import sklearn
from sklearn.manifold import TSNE
import matplotlib.pyplot as plt
import seaborn as sns

class Sampling(tf.keras.layers.Layer):
    def call(self, inputs):
        z_mean, z_log_var = inputs
        batch = tf.shape(z_mean)[0]
        dim = tf.shape(z_mean)[1]
        epsilon = tf.random.normal(shape=(batch, dim))
        return z_mean + tf.math.exp(.5 * z_log_var) * epsilon

class VAE(tf.keras.models.Model):
    def __init__(self, seq_len, input_dim, hidden_dim, beta=10):
        super(VAE, self).__init__()
        self.hidden_dim = hidden_dim
        self.beta = beta
        self.encoder = tf.keras.models.Sequential([
            tf.keras.layers.Input(shape=(seq_len, input_dim)),
            tf.keras.layers.Masking(mask_value=-1e9),
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
        with tf.GradientTape() as tape:
            z_mean, z_log_var = self.encode(x)
            z = self.reparameterize(z_mean, z_log_var)
            x_reconstr = self.decoder(z)
            x_masked = tf.boolean_mask(x, tf.not_equal(x, -1e9))
            x_reconstr_masked = tf.boolean_mask(x_reconstr, tf.not_equal(x, -1e9))
            reconstr_loss = tf.math.reduce_mean(tf.keras.losses.binary_crossentropy(x_masked, x_reconstr_masked))
            kl_loss = -.5 * (1 + z_log_var - tf.math.square(z_mean) - tf.math.exp(z_log_var))
            kl_loss = tf.math.reduce_mean(tf.math.reduce_sum(kl_loss, axis=1))
            total_loss = reconstr_loss + self.beta * kl_loss
        gradients = tape.gradient(total_loss, self.trainable_variables)
        self.optimizer.apply_gradients(zip(gradients, self.trainable_variables))
        self.train_loss(total_loss)
        self.reconstr_loss_train(reconstr_loss)
        self.kl_loss_train(kl_loss)
        return {
            'loss': self.train_loss.result(),
            'reconstr_loss': self.reconstr_loss_train.result(),
            'kl_loss': self.kl_loss_train.result()
        }

    @tf.function
    def test_step(self, x):
        z_mean, z_log_var = self.encode(x)
        x_reconstr = self.decoder(z_mean)
        x_masked = tf.boolean_mask(x, tf.not_equal(x, -1e9))
        x_reconstr_masked = tf.boolean_mask(x_reconstr, tf.not_equal(x, -1e9))
        reconstr_loss = tf.math.reduce_mean(tf.keras.losses.binary_crossentropy(x_masked, x_reconstr_masked))
        kl_loss = -.5 * (1 + z_log_var - tf.math.square(z_mean) - tf.math.exp(z_log_var))
        kl_loss = tf.math.reduce_mean(tf.math.reduce_sum(kl_loss, axis=1))
        total_loss = reconstr_loss + self.beta * kl_loss
        self.dev_loss(total_loss)
        return {
            'loss': self.dev_loss.result(),
        }

def train_autoencoder(x, hidden_dim):
    batch_size = 10
    _, seq_len, input_dim = x['train'].shape

    train_dset = tf.data.Dataset.from_tensor_slices(x['train']).batch(batch_size)
    dev_dset = tf.data.Dataset.from_tensor_slices(x['dev']).batch(batch_size)

    vae = VAE(seq_len, input_dim, hidden_dim, beta=10)
    vae.compile(optimizer='adam')
    callbacks = [
        tf.keras.callbacks.EarlyStopping(patience=10, min_delta=1e-3, verbose=1),
        tf.keras.callbacks.ModelCheckpoint('models/tmp.h5', save_best_only=True, verbose=1)
    ]
    #vae.fit(x=train_dset, epochs=1000, shuffle=True, validation_data=dev_dset, callbacks=callbacks, verbose=1)
    vae(x['train'])
    vae.load_weights('models/tmp.h5')
    return vae

def classifier_eval(x, y, vae):
    batch_size = 10
    z = {'train': vae.encode(x['train'])[0], 'dev': vae.encode(x['dev'])[0]}
    print(z['train'])
    _, input_dim = z['train'].shape
    n_classes = y['train'].max() + 1

    train_dset = tf.data.Dataset.from_tensor_slices((z['train'], y['train'])).batch(batch_size)
    dev_dset = tf.data.Dataset.from_tensor_slices((z['dev'], y['dev'])).batch(batch_size)

    model = tf.keras.models.Sequential([
        tf.keras.layers.Input(shape=(input_dim,)),
        tf.keras.layers.Dense(16, activation='relu'),
        tf.keras.layers.Dense(n_classes, activation='softmax')
    ])
    model.compile(optimizer='adam', loss='sparse_categorical_crossentropy', metrics=['accuracy'])
    callbacks = [
        tf.keras.callbacks.EarlyStopping(patience=5, verbose=1),
        tf.keras.callbacks.ModelCheckpoint('models/tmp.h5', save_best_only=True, verbose=1)
    ]
    model.fit(x=train_dset, epochs=1000, shuffle=True, validation_data=dev_dset, callbacks=callbacks, verbose=1)
    model.load_weights('models/tmp.h5')
    model.evaluate(x=dev_dset, verbose=1)

def viz(x, vae):
    z, _ = vae.encode(x['train'])
    z_transform = TSNE().fit_transform(z)
    mapping = {0: 'idle', 1: 'reach/put', 2: 'pick/retract'}
    y_ = [mapping[elem] for elem in y['train']]
    sns.scatterplot(z_transform[:,0], z_transform[:,1], hue=y_)
    plt.show()

if __name__ == '__main__':
    class Args:
        def __init__(self):
            self.subsample = 9
            self.dynamic_only = True
            self.train_sequencing = 'actions'
            self.dev_sequencing = 'actions'
            self.test_sequencing = 'actions'
            self.label_method = 'hand_motion_rhand'
            self.features = ['velY:RightHand', 'relVelZ:RightHand']

            self.output_type = 'classifier'
            self.preprocessing = ['robust', 'min-max']
    args = Args()
    nbc_wrapper = NBCWrapper(args)
    x, y = nbc_wrapper.x, nbc_wrapper.y
    vae = train_autoencoder(x, 3)
    viz(x, vae)
    classifier_eval(x, y, vae)
