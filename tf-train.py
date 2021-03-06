from pathlib import Path

from predictor.models import TFAkiBase, TFAkiLstm, TFAkiGpt2

import fire
import logging
import numpy as np
import tensorflow as tf

# set random seed (for reproducibility)
np.random.seed(7)
tf.random.set_seed(7)

# constants
TIMESTEPS = 8
N_FEATURES = 16


def train_models(
    epochs: int = 1,
    batch_size: int = 256,
    dataset_dir: str = 'dataset',
    ckpt_dir: str = 'saved_models',
    log_dir: str = 'logs',
    training: str = 'matrix_training.npy',
    val: str = 'matrix_validation.npy',
):
    '''
    Trains 3 models (base LSTM, LSTM with attn, and GPT-2) to predict next-day AKI.

    Parameters:
    epochs: For how many epochs to train the models
    batch_size: The batch size to be used during training (the bigger the better)
    dataset_dir: The name of the directory that contains the training and validation 
        datasets (stored in .npy files)
    ckpt_dir: The name of the directory where the checkpoints of the trained models 
        are saved (only the best weights are saved).
    training: The filename of the training dataset to be used (should be a file 
        serialized using np.save and with a shape of [n_samples, timesteps, n_features + 1]
        where 1 refers to the AKI prediction labels)
    val: The filename of the validation dataset to be used (should be a file 
        serialized using np.save and with a shape of [n_samples, timesteps, n_features + 1]
        where 1 refers to the AKI prediction labels)
    '''
    # check cuda availability
    devices = tf.config.list_physical_devices('GPU')
    if not devices:
        print('CUDA is not available. Training will be slow.')

    # convert dir names to dir paths
    dataset_path = Path(dataset_dir)
    ckpt_path = Path(ckpt_dir)
    log_path = Path(log_dir)

    # verify training and validation data's existence
    train_path = dataset_path / training
    val_path = dataset_path / val
    assert train_path.exists(), f'{training} does not exist'
    assert val_path.exists(), f'{val} does not exist'

    # load training and validation data
    train_matrix = np.load(train_path).astype(np.float32)
    train_x = train_matrix[:, :, :-1]
    train_y = train_matrix[:, :, -1:]
    val_matrix = np.load(val_path).astype(np.float32)
    val_x = val_matrix[:, :, :-1]
    val_y = val_matrix[:, :, -1:]

    # prepare training keyword arguments
    # same arguments to be used by the 3 models
    training_kwargs = {
        'x': train_x,
        'y': train_y,
        'epochs': epochs,
        'batch_size': batch_size,
        'shuffle': True,
        'validation_data': (val_x, val_y),
    }

    # train all models
    train('base', training_kwargs, ckpt_path=ckpt_path, log_path=log_path)
    train('lstm', training_kwargs, ckpt_path=ckpt_path, log_path=log_path)
    train('gpt2', training_kwargs, ckpt_path=ckpt_path, log_path=log_path)


def train(name: str, training_kwargs, *, ckpt_path: Path, log_path: Path):
    '''
    Creates and train a specific model. The best weights (roc auc score on the 
    validation set is monitored) of the model is saved on the directory `ckpt_path`.

    Parameters:
    name: The name of the model to be trained (base, lstm, gpt2).
    training_kwargs: The dictionary of all the training parameters for `model.fit`.
    ckpt_path: The name of the directory where checkpoints will be saved.
    log_path: The name of the directory where tensorboard stuff will be saved.
    '''
    # create the model (from scratch) to be trained
    model = get_model(name)

    # we use the default adam optimizer
    # the loss function and metrics are defined for output 1 (predictions)
    # None are given to output 2 (since it's just the attn weights)
    model.compile(
        optimizer='adam',
        loss=['binary_crossentropy', None],
        metrics=[['acc', tf.keras.metrics.AUC(name='auc')], None],
    )

    # setup training callbacks (logging and checkpoints)
    tb_callback = tf.keras.callbacks.TensorBoard(
        log_dir=log_path / name,
        histogram_freq=1,
        write_graph=False,
        profile_batch=0,
    )

    # setup checkpoint callback (only saving the best weights
    # according to the validation set's ROC AUC score
    model_name = f'{name}_e{training_kwargs["epochs"]}'
    model_weights_path = ckpt_path / model_name / name
    ckpt_callback = tf.keras.callbacks.ModelCheckpoint(
        model_weights_path,
        monitor='val_auc' if name == 'base' else 'val_output_1_auc',
        verbose=1,
        save_best_only=True,
        mode='max',
        save_weights_only=True,
    )

    # train model with tensorboard callback (for graphing)
    model.fit(
        callbacks=[tb_callback, ckpt_callback],
        **training_kwargs,
    )


def get_model(name: str):
    if name == 'base':
        return TFAkiBase()

    if name == 'lstm':
        return TFAkiLstm(
            timesteps=TIMESTEPS,
            n_features=N_FEATURES,
        )

    if name == 'gpt2':
        return TFAkiGpt2(
            n_heads=2,
            timesteps=TIMESTEPS,
            n_features=N_FEATURES,
        )

    raise AssertionError(f'Unknown model "{name}"')


if __name__ == '__main__':
    fire.Fire(train_models)
