#!/usr/bin/env python
# coding: utf-8

###general imports
import copy
import os
import shutil

import matplotlib.pyplot as plt
import pandas as pd
import numpy as np
from sklearn.metrics import classification_report, f1_score
import keras
import keras.backend as K
import tensorflow as tf
from tensorflow.keras import optimizers, layers
from tensorflow.keras import losses
from tensorflow.keras import callbacks
from tensorflow.keras.layers.experimental import preprocessing
from tensorflow.keras.models import Model
import pickle
import time
import csv
from functools import partial
from sklearn.utils import class_weight
from sklearn.preprocessing import StandardScaler, MinMaxScaler
import scipy.stats as stats
import tensorflow_addons as tfa
import seaborn as sns
import scipy
import warnings
import rasterio
import math
# from tensorflow.python.framework.ops import disable_eager_execution
# disable_eager_execution()
# warnings.filterwarnings("ignore")
# tf.autograph.set_verbosity(1)
import sys

import config
###priv imports
import config as cfg
import visualizations
import nn_utils as nnu
import nn_models as nnm
import helper_utils as hu
# import geo_utils as gu

#####This needs to be done before using tensorflow or keras (e.g. importing losses in config)
###Some general information and settings
# print some general information
print('keras v', keras.__version__)
print('tf keras v', tf.keras.__version__)
print('tf v', tf.__version__)
# to do: try non eager execution graph?
print('tf eager execution', tf.executing_eagerly())
# force channels-first ordering
K.set_image_data_format('channels_first')
print('Image data format', K.image_data_format())

#turn off cryptic warnings, Note that you might miss important warnings! If unexpected stuff is happening turn it on!
#https://github.com/tensorflow/tensorflow/issues/27023
#Thanks @Mrs Przibylla
#'1' = Infos, '2' = warnings, '3' = Errors
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '2'

# do not assign complete gpu-memory but grow it as needed
# allows to run multiple models at once (otherwise whole gpu memory gets allocated/gpu gets blocked)
gpus = tf.config.experimental.list_physical_devices('GPU')
if gpus:
    try:
        for gpu in gpus:
            tf.config.experimental.set_memory_growth(gpu, True)
    except RuntimeError as e:
        raise e


###Define Model and MirroredStrategy (for mulit-gpu usage) - shall be initiated at the beginning of the program
# Create a MirroredStrategy and pass the GPUs
gpus = ["GPU:" + str(i) for i in cfg.gpus]
# https://keras.io/guides/distributed_training/
print('gpus', gpus)
strategy = tf.distribute.MirroredStrategy(gpus)
print('Number of GPUs MirroredStrategy: {}'.format(strategy.num_replicas_in_sync))


def create_run_folders(augmentation_d, img_path, prj_path, trainHistory_subname, label_name=False):
    """Creates a name for the model run by importing settings from config.py.
    This will also be used as the folder name where all files of this run are saved.
    Creates this folder and further subfolders.

    Note that config.py also gets replicated into the run_path for easy replication and exact configuration.
    Also imports some parameters directly from config.py

    Args:
        augmentation_d (dict): Augmentation parameters (as defined in config.py)
        normalization_d (dict): Normalization parameters (as defined in config.py)
        prj_path (str): Path where the project is located
        trainHistory_subname (str): Name of the folder where all model runs will are saved

    Returns:
        train_history_path (str): Path where all model runs are saved
        run_path (str): Path where all model run files are saved (subfolder of train_history_path)
        modelcheckpoint_path (str): Path where all modelcheckpoint files are saved (subfolder of run_path)
        augmented_images_path (str): Path where augmented images get saved in jpg-format for easy interpretation of
            input and augmentation (if activated in config.py) (subfolder of run_path)
    """

    ###Create global paths
    # Create train history path name //Run name
    img_p = os.path.basename(img_path[:-1])
    img_p = img_p.replace(',', '_')
    img_p = img_p.replace(' ', '_')
    aug_name = '_'
    for k, v in augmentation_d.items():
        if v is False or v == 0 or v == {}:
            pass
        elif v is True:
            aug_name += k + '_'
        else:
            aug_name += k + str(v)
    # replace some special chars and omit some strings for better readability and acceptable length
    aug_name = aug_name.replace(',', '_')
    aug_name = aug_name.replace('range', '')
    aug_name = aug_name.replace('_center_featurewise_std_normalization_', '')
    aug_name = aug_name.replace('_center_samplewise_std_normalization_', '')
    # strip every char containing harmful chars for paths
    aug_name = aug_name.translate({ord(c): None for c in '[],;!@#$() '})

    # create base name
    run_name = img_p + cfg.model_name + cfg.run_name_custom_string + '_w' + \
               str(cfg.cnn_settings_d['weights']) + '_unfl' + str(cfg.unfreeze_layers_perc) + \
               '_d' + str(cfg.dropout_top_layers) + '_lr' + str(cfg.lr)
    if cfg.auto_adjust_lr[0]:
        run_name += '_adjustlr'
    if cfg.momentum:
        run_name += '_momentum' + str(cfg.momentum)
    run_name += '_optimizer' + cfg.optimizer + aug_name
    run_name += '_m_' + cfg.model_name

    sample_string = label_name
    char_to_replace = {' ': '_',
                       '<': '_',
                       '=': '_',
                       ':': '_',
                       '(': '',
                       ')': ''}
    # Iterate over all key-value pairs in dictionary
    for key, value in char_to_replace.items():
        # Replace key character with value character in string
        sample_string = sample_string.replace(key, value)
        run_name = run_name.replace(key, value)

    # Create paths II
    paths_dict = {'train_history_path': ['base_path', trainHistory_subname, False],
                  'run_path': ['train_history_path', run_name, True],
                  'modelcheckpoint_path': ['run_path', 'modelcheckp/', False],
                  'augmented_imgs_path': ['run_path', 'augmented_imgs/', False]}

    [train_history_path, run_path, modelcheckpoint_path, augmented_images_path] = \
        hu.paths_from_base_path(prj_path, paths_dict, add_d={'train_history_path': sample_string + '/'})
    run_name += run_path[-2:]
    return train_history_path, run_path, modelcheckpoint_path, augmented_images_path, run_name


def optimizer_loading():
    """Summarizing optimizer loading to simplify code in main

    Loads optimizer (str) from config.py and

    Returns:
        optimizer (Keras optimizer): ...
    """
    # Optimizers
    if cfg.optimizer == "SGD":
        # from transferLearning.py
        optimizer = tf.keras.optimizers.SGD(learning_rate=cfg.lr, momentum=cfg.momentum, nesterov=False,
                                            name="SGD")
    elif cfg.optimizer == "Adam":
        # from transferLearning.py
        optimizer = optimizers.Adam(lr=cfg.lr, beta_1=0.9, beta_2=0.999, epsilon=1e-08, decay=0.0)
    else:
        raise NotImplementedError('cfg.optimizer is', cfg.optimizer,
                                  'it is not implemented right now though')
    return optimizer


def callback_loader(run_path, modelcheckpoint_path, metric):
    """Summarizing callback loading to simplify code in main

    Loads auto_adjust_lr, early_stopping and tensorboard variables from config.py

    Args:
        run_path (str): Path where run files are stored
        modelcheckpoint_path (str) Path where model checkpoints are stored

    Returns:
        callbacks_l (list): List of callback objects as passed to model.fit()
    """
    # Callbacks
    callbacks_l = []
    if cfg.auto_adjust_lr[0]:
        # Reduces the learning rate by some factors if the validation loss stops improving in some epochs
        reduce = callbacks.ReduceLROnPlateau(
            monitor='val_' + metric.name, factor=cfg.auto_adjust_lr[2],
            patience=cfg.auto_adjust_lr[1], mode='auto')
        callbacks_l.append(reduce)
    if cfg.early_stopping[0]:
        # Stops the training process if the validation loss stops improving in some epochs (here 10)
        early = callbacks.EarlyStopping(monitor='val_' + metric.name,
                                        min_delta=1e-4, patience=cfg.early_stopping[1],
                                        mode='auto')  # mode=max, restore_best_weights=True
        callbacks_l.append(early)
    if cfg.tensorboard:
        callbacks_l.append(callbacks.TensorBoard(log_dir=run_path,
                                                 histogram_freq=0,
                                                 write_graph=True,
                                                 write_images=False,
                                                 write_steps_per_second=True,
                                                 update_freq="epoch",
                                                 profile_batch=0,
                                                 embeddings_freq=0,
                                                 embeddings_metadata=None, ))

    callbacks_l.append(callbacks.ModelCheckpoint(modelcheckpoint_path,
                                                 monitor='val_' + metric.name,
                                                 verbose=1,
                                                 save_weights_only=True,
                                                 save_best_only=True))
    return callbacks_l


def model_loader(num_labels, type_m, shape, **aug_d):
    """Summarizing model loading from string defined in config.py to simplify code in main
    Also loads lots of variables from config.py, especially: cnn_settings_d (and many more)
    If defined in config.py add_custom_top_layers it will also add custom top layers by calling add_top_layer from
    nn_models.py!

    Returns:
        model (Keras model): e.g. VGG19 or resnet50 if defined with custom top layers
    """
    cnn_settings_d = cfg.cnn_settings_d
    cnn_settings_d['input_shape'] = shape
    print('cnn settings d', cnn_settings_d)
    if cfg.model_name == 'resnet152':
        base_model = tf.keras.applications.ResNet152V2(**cnn_settings_d)
    elif cfg.model_name == 'resnet50':
        base_model = tf.keras.applications.ResNet50V2(**cnn_settings_d)
    elif cfg.model_name == 'vgg16':
        base_model = tf.keras.applications.VGG16(**cnn_settings_d)
    elif cfg.model_name == 'vgg19':
        base_model = tf.keras.applications.VGG19(**cnn_settings_d)
    elif cfg.model_name == 'inceptionv3':
        base_model = tf.keras.applications.InceptionV3(**cnn_settings_d)
    elif cfg.model_name == 'densenet121':
        base_model = tf.keras.applications.DenseNet121(**cnn_settings_d)
    elif cfg.model_name == 'densenet201':
        base_model = tf.keras.applications.DenseNet201(**cnn_settings_d)
    elif cfg.model_name == 'xception':
        base_model = tf.keras.applications.Xception(**cnn_settings_d)
    else:
        raise NotImplementedError("The model you want is not implemented right now", cfg.model_name)


        # base_model = Model(inputs, outputs)
    # Add custom Top Layers (Imagenet has 1000nds of classes - we need 3!)
    if cnn_settings_d['include_top'] is False and cfg.add_custom_top_layers:
        base_model = nnm.add_classification_top_layer(base_model, num_labels, cfg.neurons_l, type_m,
                                                      cfg.dropout_top_layers, cfg.unfreeze_layers_perc)
        print('Final Model with added top layers', type(base_model), 'layers', len(base_model.layers),
              'of which', round(len(base_model.layers) * cfg.unfreeze_layers_perc / 100), 'are unfroozen or ',
              cfg.unfreeze_layers_perc, '%')
    #Add preprocessing layers
    if aug_d:
        input_layer = tf.keras.Input(shape=shape)
        aug_layer = augmentation(**aug_d)
        x = aug_layer(input_layer)
        x = base_model(x)
        base_model = tf.keras.Model(inputs=input_layer, outputs=x)
        print('final model', base_model)
    return base_model


def return_geotiff_as_array(file):
    if isinstance(file, tf.Tensor):
        file = bytes.decode(file.numpy())
    with rasterio.open(file) as img:
        return img.read().astype("float32")


def return_geotiff_from_tensor(file):
    file = bytes.decode(file.numpy())
    with rasterio.open(file) as img:
        img = img.read().astype("float32")
        # height, width, channels = img.shape
        # img = tf.reshape(img, [height, width, channels])
    return img


def dataset_creator(train_df, validation_df, test_df, prediction_type, num_labels, cache_p, **augargs):
    ds_l = []
    for typ, df in zip(['train', 'validation', 'test'], [train_df, validation_df, test_df]):
        #shuffle in pandas
        df = df.sample(frac=1)
        print(df)
        if typ == 'train':
            steps_per_epoch_train = math.ceil(len(df)/cfg.batch_size)
        elif typ == 'validation':
            steps_per_epoch_val = math.ceil(len(df) / cfg.batch_size)
        elif typ == 'test':
            steps_per_epoch_test = math.ceil(len(df) / cfg.batch_size)
        files = tf.data.Dataset.from_tensor_slices(tf.constant(df['path']))
        labels = np.array(df['label'])
        if prediction_type == 'regression':
            labels = labels.reshape(-1, 1)
        elif prediction_type == 'categorical':
            labels = tf.constant(labels)
            labels = tf.one_hot(labels, num_labels)
        else:
            raise ValueError(f"cfg.type_m or respectively prediction_type must be 'regression' or 'categorical' it is"
                             f" {prediction_type} though")
        labels = tf.data.Dataset.from_tensor_slices(labels)
        assert len(files) == len(labels)
        if cfg.verbose:
            print('files ds', files)
            print('labels ds', labels)
        ds = files.map(lambda x: tf.py_function(return_geotiff_from_tensor, [x], tf.float32),
                    num_parallel_calls=tf.data.AUTOTUNE)
        if cfg.verbose:
            print('Read imgs ds', ds)
            # for x in ds.take(1):
            #     print('Imgs', x.get_shape(), type(x))
            # for x in labels.take(1):
            #     print('Labels', x.get_shape(), type(x))
        assert len(ds) == len(labels)
        ds = tf.data.Dataset.zip((ds, labels))
        #shuffling before caching eats up memory like crazy?
        # if typ == 'train':
        #     ds = ds.shuffle(math.floor(len(ds)/5), reshuffle_each_iteration=False)
        if cfg.verbose:
            print('Zipped ds', ds, len(ds))
            # for x, y in ds.take(1):
            #     print('Imgs', x.get_shape(), type(x))
            #     print('Labels', y.get_shape(), type(y))
        #significantly improves speed! ~25%
        if typ == 'train' or typ == 'validation':
            cache_f = cache_p + typ
            ds = ds.cache(cache_f)
            #buffer_size does not seem to influence the performance
            ds = ds.shuffle(cfg.batch_size*2, reshuffle_each_iteration=True)
        ds = ds.batch(cfg.batch_size)
        #does not seem to have an influence when used with cached datasets
        ds = ds.prefetch(tf.data.AUTOTUNE)
        # if not typ == 'test':
        #     ds = ds.repeat()
        if cfg.verbose:
            print('Final ds', ds)
            # if typ == 'train':
            #     for x, y in ds.take(1):
            #         print('Imgs', x.get_shape(), type(x))
            #         print('Labels', y.get_shape(), type(y))
        ds_l.append(ds)
        # input('created')
    # steps_per_epoch_train = False
    # steps_per_epoch_val = False
    return ds_l[0], ds_l[1], ds_l[2], steps_per_epoch_train, steps_per_epoch_val, steps_per_epoch_test


def special_replace(row, replace_d, col, labels_df, drop):
    """Experimental and seldom used: replacing labels by others in"""
    replaced = False
    if 'urban' in cfg.labels_f:
        if col == 'source of drinking water (weights): max':
            if row[0] == 1.0:
                replace_d[row[0]] = 2.0
                replaced = True
                print('replaced label:', row[0], 'n:', row[1][col], '< threshold', len(labels_df) * drop,
                      'with 2')
    return replace_d, replaced


def load_labels(file, col, base_path=False, mode='categorical', drop=0.05, min_year=False,
                split='random split', map_drop_to_other=False, special_rep=False, break_v=4000, **kwargs):
    """
    Loads, manipulates and one-hots labels

    Args:
        file: csv file with labels (min information: path to .tif OR DHSID AND GEID, label, train, validation test split
            in one column)
        col: column to use as label
        base_path: if there is no path in the label file, matches .tif names with DHSID and GEID to files in this folder
        mode: 'categorical' or 'regression' defines type of model and prepares labels accordingly
        drop: drops labels below a certain share of the dataset (e.g. 0.05)
        split: defines the column name of the label file where the split is defined
        map_drop_to_other: maps labels which would get dropped to 'other' category instead of dropping them
        special_rep: depreceated?!? needs to be checked!!!
        break_v: throws a warning if the amount of label data falls below a threshold (default=4000)
        **kwargs: (can be specified in the config and passed here)
        'min value': Float: maps (or drops) labels below the value to min value
        'max value': Float: maps (or drops) labels above the value to max value
        'drop min max value': use 'True' to drop the values below/above the 'min value' and 'max value'
        'reverse label': use 'True' to multiply label by -1
        'label normalization': 'Z' or '0,1' label normalization
        'label transform': 'boxcox' transforms the label by boxcox transformation (note 0 is shifted to 0.1)

    Returns:
        datagen_train, datagen_val, datagen_test (Keras ImageDataGenerator): ...
    """
    reverse_label, min_value, max_value, drop_min_max_value, normalization, transform = False, False, False, False, \
                                                                                                     False, False
    for k, v in kwargs.items():
        if k == 'reverse label':
            reverse_label = v
        elif k == 'min value':
            min_value = v
        elif k == 'max value':
            max_value = v
        elif k == 'drop min max value':
            drop_min_max_value = v
        elif k == 'label normalization':
            normalization = v
        elif k == 'label transform':
            transform = v
    print('drop', drop_min_max_value)
    print('minmax value', min_value, max_value)
    labels_df = pd.read_csv(file)
    # for c in labels_df.columns:
    #     print(labels_df[c].value_counts(dropna=False))
    # sys.exit()
    if base_path:
        #create pathes
        labels_df['path'] = base_path + labels_df['GEID'] + labels_df['DHSID'].str[-8:] + '.tif'
        available_files = hu.files_in_folder(base_path)
        # print(base_path)
        # print('generated pathes', labels_df['path'])
        # print('available files in folder', available_files)
        #check if actually available
        labels_df["path"] = labels_df['path'].apply(lambda x: x if x in available_files else np.NaN)
    if min_year:
        print(f"If not using min year {min_year} {len(labels_df[labels_df['DHSYEAR'] < min_year])} more data could be "
              f"used")
        labels_df = labels_df[labels_df['DHSYEAR'] >= min_year]
    labels_df = labels_df[['GEID', 'DHSID', 'path', split, col]]
    labels_df['label'] = labels_df[col]
    #print(labels_df)
    if labels_df.isnull().any(axis=1).any():
        print(labels_df[labels_df.isnull().any(axis=1)])
        warnings.warn(f"!!!Caution Missing values getting dropped {len(labels_df[labels_df.isnull().any(axis=1)])}"
                      f" from which are {len(labels_df[labels_df['label'].isna()])} missing labels and "
                      f"{len(labels_df[labels_df['path'].isna()])} missing files \n"
                      f"--> writing missing files into {cfg.prj_folder + 'missing_files.csv'} "
                      f" if these have labels")

        if not labels_df[(labels_df['path'].isna()) & (labels_df['label'].notna())].empty:
            labels_df[(labels_df['path'].isna()) & (labels_df['label'].notna())].to_csv(cfg.prj_folder + 'missing_files.csv')
    labels_df = labels_df.dropna()
    labels_df['label'] = labels_df[col]
    print(labels_df)
    label_mapping = {}
    add_params = {}
    scaler = False
    for c in labels_df.columns:
        print('labels_df value counts', labels_df[c].value_counts(dropna=False))
    if mode == 'categorical':
        replace_d = {}
        values_df = pd.DataFrame(labels_df[col].value_counts())
        for nr, row in enumerate(values_df.iterrows()):
            if row[1][col] > len(labels_df) * drop:
                replace_d[row[0]] = nr
                if cfg.verbose:
                    print('label:', row[0], 'n:', row[1][col])
            else:
                replaced = False
                if special_rep:
                    replace_d, replaced = special_replace(row, replace_d, col, labels_df, drop)
                if map_drop_to_other and not replaced:
                    replace_d[row[0]] = 'other'
                    if cfg.verbose:
                        print('replaced label:', row[0], 'n:', row[1][col], '< threshold', len(labels_df) * drop,
                              'with "other"')
                else:
                    replace_d[row[0]] = np.NaN
                    if cfg.verbose:
                        print('dropped label:', row[0], 'n:', row[1][col], '< threshold', len(labels_df) * drop)
        labels_df = labels_df.replace({'label': replace_d})
        labels_df = labels_df.dropna()
        labels_df['label'] = labels_df['label'].astype(int)
        label_mapping = {v: k for k, v in replace_d.items() if not np.isnan(v)}
        # drop
    elif mode == 'regression':
        print('Some basic statistics before normalization/transformation:')
        add_params = {'mean': labels_df['label'].mean(), 'std': labels_df['label'].std(),
                      'skew': labels_df['label'].skew()}
        print('min', min(labels_df['label']))
        print('max', max(labels_df['label']))
        print('std deviation', labels_df['label'].std())
        print('mean', labels_df['label'].mean())
        print('skew', labels_df['label'].skew())
        if reverse_label:
            labels_df['label'] = labels_df['label'] * (-1.0)
            labels_df['reversed'] = labels_df['label']
        for val in [min_value, max_value]:
            if val is not False:
                # get rid of outlier
                print('min/max val', val)
                #setting to min/max value
                if val > 0:
                    print(labels_df[labels_df['label'] >= val]['label'])
                    labels_df['label'] = np.where(labels_df['label'].between(val, 99999999999), val,
                                              labels_df['label'])
                else:
                    print(labels_df[labels_df['label'] <= val]['label'])
                    labels_df['label'] = np.where(labels_df['label'].between(-99999999999, val), val,
                                              labels_df['label'])
                print(labels_df[labels_df['label'] == val]['label'])
                #dropping
                if drop_min_max_value:
                    labels_df['label'] = labels_df['label'].replace({val: np.NaN})
                    print('dropping due to min/max value', val, len(labels_df[labels_df['label'].isna()]))
                    labels_df = labels_df.dropna()
        if transform == 'boxcox':
            #boxcox doesnt work with 0
            # replace_d = {'label': {0: 0.1}}
            min_v = min(labels_df['label'])
            if min_v <= 0:
                shift_v = abs(min_v) + 1
                add_params['shift value'] = shift_v
                #right shift values (boxcox cant handle negative or 0 values)
                labels_df['label'] = labels_df['label'] + shift_v
            # labels_df = labels_df.replace(replace_d)
            array, lmbda = stats.boxcox(labels_df['label'])
            add_params['lmbda'] = lmbda
            labels_df['label'] = array
        if transform:
            add_params['skew after transforming'] = labels_df['label'].skew()
            print('Some basic statistics after transforming w', transform, ':')
            print('min', min(labels_df['label']))
            print('max', max(labels_df['label']))
            print('std deviation', labels_df['label'].std())
            print('mean', labels_df['label'].mean())
            print('skew', labels_df['label'].skew())
            labels_df['transformed'] = labels_df['label']

        if normalization:
            add_params['normalization mean'] = labels_df['label'].mean()
            add_params['normalization standard deviation'] = labels_df['label'].std()
            if normalization == '0,1':
                scaler = MinMaxScaler()
                labels_df['label'] = scaler.fit_transform(np.array(labels_df['label']).reshape(-1, 1))
            elif normalization == 'Z':
                scaler = StandardScaler()
                labels_df['label'] = scaler.fit_transform(np.array(labels_df['label']).reshape(-1, 1))
            else:
                raise NotImplementedError
            add_params['skew after normalization'] = labels_df['label'].skew()
            add_params['mean after normalization'] = labels_df['label'].mean()
            print('Some basic statistics after', normalization, ':')
            print('min', min(labels_df['label']))
            print('max', max(labels_df['label']))
            print('std deviation', labels_df['label'].std())
            print('mean', labels_df['label'].mean())
            print('skew', labels_df['label'].skew())
            labels_df['normalized'] = labels_df['label']
        else:
            print('Did not normalize')

    train_df = labels_df[labels_df[split] == 'train']
    validation_df = labels_df[labels_df[split] == 'validation']
    test_df = labels_df[labels_df[split] == 'test']
    if cfg.test_mode:
        train_df = train_df[:cfg.test_mode]
        validation_df = validation_df[:math.floor(cfg.test_mode/10)]
        test_df = test_df[:math.floor(cfg.test_mode/10)]
        print('testing mode!!! Limited train df:', len(train_df))
    labels_amount = {'data amount': len(labels_df),
                     'train amount': len(train_df), 'train percentage': len(train_df)/len(labels_df),
                     'validation amount': len(validation_df), 'validation percentage': len(validation_df) / len(labels_df),
                     'test amount': len(test_df), 'test percentage': len(test_df) / len(labels_df)}
    for k, v in labels_amount.items():
        print(k, v)
    print('Input Df')
    print(labels_df['label'])
    print(labels_df.count())
    for st, df in zip(['all labels', 'train', 'validation', 'test'], [labels_df, train_df, validation_df, test_df]):
        if st == 'validation' or st == 'test':
            if len(df) / len(labels_df) <= 0.09 or len(df) / len(labels_df) >= 0.15:
                print(st, df)
                warnings.warn(f'CAUTION!!!: DF has critically low or high amount of values {len(df) / len(labels_df)}')
    if len(labels_df['label']) <= break_v:
        warnings.warn('WARNING!!!: length of labels is below', break_v, 'len labels', len(labels_df['label']))
    return train_df, validation_df, test_df, labels_df, label_mapping, labels_amount, add_params, scaler


def reverse_norm_transform(df, normalization, transform, scaler, additional_params, run_path, **kwargs):
    for k, v in kwargs.items():
        if k == 'label normalization':
            normalization = v
        elif k == 'label transform':
            transform = v
    print('transform', transform)
    print('normalization', normalization)
    new_df = df
    for col in new_df.columns:
        if normalization:
            if normalization == 'Z':
                new_df[col] = scaler.inverse_transform(np.array(new_df[col]).reshape(-1, 1))
                    # reverse_zscore(new_df[col], additional_params['normalization mean'],
                    #                      additional_params['normalization standard deviation'])
            elif normalization == '0,1':
                new_df[col] = scaler.inverse_transform(np.array(new_df[col]).reshape(-1, 1))
            else:
                raise NotImplementedError('Did not implement', normalization)
            visualizations.histogram(new_df[col], 'probability', col + ' denormalized', run_path, col + ' denormalized')

        if transform:
            if transform == 'boxcox':
                new_df[col] = scipy.special.inv_boxcox(new_df[col], additional_params['lmbda'])
                if 'shift value' in additional_params:
                    new_df[col] = new_df[col] - additional_params['shift value']
            else:
                raise NotImplementedError('Did not implement', transform)
            visualizations.histogram(new_df[col], 'probability', col + ' detransformed', run_path, col + ' detransformed')
    return new_df


def augmentation(**kwargs):
    layer_l = []
    for k, v in kwargs.items():
        if k == 'horizontal_flip' and v == True:
            layer_l.append(preprocessing.RandomFlip("horizontal"))
        elif k == 'vertical_flip' and v == True:
            layer_l.append(preprocessing.RandomFlip("vertical"))
        elif k == 'zoom_range':
            layer_l.append(preprocessing.RandomZoom(height_factor=(-v, v)))
        elif k == 'rotation_range':
            layer_l.append(preprocessing.RandomRotation((-v, v)))
        else:
            raise NotImplementedError()
    data_augmentation_layers = False
    if layer_l:
        print('Augmentation layers', layer_l)
        data_augmentation_layers = tf.keras.Sequential(layer_l)
        print(data_augmentation_layers)
    return data_augmentation_layers


def create_visualisations(model, test_ds, steps_per_epoch_test, run_path, label_mapping, add_params, labels_df, scaler,
                        label_d, add_name=''):
    beta, alpha, pearson_corr, rmse = False, False, False, False
    # Create Confusion matrix for the test dataset
    # Get np array of predicted labels and true labels for test dataset
    test_pred = model.predict(test_ds, steps=steps_per_epoch_test)
    test_true = np.concatenate([y for x, y in test_ds], axis=0)
    if cfg.type_m == 'categorical':
        test_pred = np.argmax(test_pred, axis=1)
        test_true = np.argmax(test_true, axis=1)
    assert len(test_pred) == len(test_true)

    with open(run_path + '/pickle_prediction_true_1', 'wb') as f:
        pickle.dump([test_pred, test_true, label_mapping, add_params, run_path, labels_df, scaler], f)

    if cfg.type_m == 'regression':
        test_prediction = pd.DataFrame({'Prediction': np.array(test_pred).reshape(-1),
                                        'Actual': np.array(test_true).reshape(-1)})
        print('final prediction', test_prediction)
        test_prediction_l = [test_prediction]
        if cfg.label_normalization or cfg.label_transform or 'label normalization' in label_d or 'transformation' in label_d:
            print('hello here')
            test_prediction_l.append(copy.deepcopy(test_prediction))
            test_prediction = reverse_norm_transform(test_prediction,
                                                     cfg.label_normalization, cfg.label_transform,
                                                     scaler,
                                                     additional_params=add_params,
                                                     run_path=run_path, **label_d)
        with open(run_path + '/pickle_prediction_true', 'wb') as f:
            pickle.dump([test_prediction, label_mapping, add_params, run_path], f)

        for name, df in zip(['Original Data', 'Model Input'], test_prediction_l):
            visualizations.scatterplotRegression(df=df, run_path=run_path, file_name=name + add_name)

            beta, alpha = np.polyfit(df.Actual, df.Prediction, 1)
            corr_value = df.corr(method="pearson")
            pearson_corr = corr_value["Prediction"][1]
            rmse = ((df['Prediction'] - df['Actual']) ** 2).mean() ** .5
            print('Further stats for test data:' + name)
            print('RMSE:', rmse)
            print('r2:', pearson_corr)
            print('beta/alpha', beta, alpha)
    sk_metrics_d = {}
    if cfg.type_m == 'categorical':
        print(len(test_pred), len(test_true))
        print('testpred', test_pred)
        print('true', test_true)
        # test_prediction = np.array(test_pred)
        # test_true = np.array(test_true)
        add_params['f1 micro'] = f1_score(test_true, test_pred, average='micro')

        # Confusion matrix
        cm_plot_labels = list(label_mapping.values())
        visualizations.plot_CM(test_true, test_pred, cm_plot_labels, run_path +
                               'ConfusionMatrix' + add_name)
        # create sklearn report (for f1 score and more)
        classification_d = classification_report(test_true, test_pred, output_dict=True,
                                                 target_names=cm_plot_labels)
        print('test classification', classification_d)
        print('f1', add_params['f1 micro'])
        # transform for one row
        for class_name, sdic in classification_d.items():
            try:
                for metric_n, v in sdic.items():
                    sk_metrics_d[class_name + ': ' + metric_n] = v
            except AttributeError:
                sk_metrics_d[class_name] = sdic
    print('Ending def')
    return beta, alpha, pearson_corr, rmse, sk_metrics_d


# if center_crop:
#     layer_l.append(tf.keras.layers.CenterCrop(center_crop[0], center_crop[1]))
#     29      layers.experimental.preprocessing.RandomRotation(0.2),
#     layers.experimental.preprocessing.RandomTranslation(0.3, 0.3, fill_mode='reflect', interpolation='bilinear', )
#     32])

# 'brightness_range': [0.9, 1.1], #Tuple or list of two floats. Range for picking a brightness shift value from.
# 'shear_range': 0.2,  # Float. Shear Intensity (Shear angle in counter-clockwise direction in degrees)
# 'channel_shift_range': 0.3,
# 'width_shift_range': 0.2,
# 'height_shift_range': 0.2

def main():
    warnings.warn(f'This model assumes that you have preprocessed images, which need to have the exact same size')
    for label_name_in, label_d in cfg.label_name.items():
        label_name = label_name_in[:-1]
        print('modelling water supply', label_name, 'type', cfg.type_m)
        # Import paths I
        prj_path = cfg.prj_folder
        paths_dict = {
            'sentinel_path': ['base_path', 'Sentinel2/', False],
            'sentinel_img_path': ['sentinel_path', 'preprocessed/water/urban/', False],
            'train_path': ['sentinel_img_path', 'train/', False],
            'val_path': ['sentinel_img_path', 'validation/', False],
            'test_path': ['sentinel_img_path', 'test/', False]}

        if cfg.load_subfolders:
            folders = hu.files_in_folder(cfg.img_path, return_folders=True)
        else:
            folders = cfg.img_path
        print('folders', folders)
        # iterating over multiple folders of differently normalized input files
        for img_path in folders:
            t_begin = time.time()

            # load labels and class_weights from file (or calculate later one)
            train_df, validation_df, test_df, labels_df, label_mapping, labels_amount, add_params, scaler = \
                load_labels(os.path.join(cfg.labels_f), label_name, base_path=img_path,
                            mode=cfg.type_m, normalization=cfg.label_normalization,
                            transform=cfg.label_transform, split=cfg.split, min_year=cfg.label_min_year, **label_d)
            #load shape
            file = train_df['path'].iloc[0]
            arr = return_geotiff_as_array(file)
            input_shape = arr.shape
            if cfg.verbose:
                print('Using following shape (must be identical for all loaded datasets)', input_shape)

            if cfg.type_m == 'categorical':
                num_labels = len(label_mapping)
                class_weights = class_weight.compute_class_weight('balanced', classes=np.unique(labels_df['label']),
                                                                  y=labels_df['label'])
                class_weights = dict(enumerate(class_weights))
            else:
                num_labels = 1
                class_weights = False

            for aug_key, augmentation_d in cfg.IDG_augmentation_settings_d.items():
                if cfg.verbose:
                    if cfg.test_mode:
                        warnings.warn("Test mode is active! Your dataset is significantly shorter")
                        print('its length is:', len(train_df))
                    print('input path', img_path)
                    print('aug_key, augmentation_d', aug_key, augmentation_d)
                    print('labels')
                    print(labels_df.head())
                    if cfg.type_m == 'categorical':
                        print('class weigths', class_weights)
                        print(label_mapping)
                        print(labels_df['label'].value_counts())
                        print(labels_df[label_name].value_counts())

                # create new folders for run files
                train_history_path, run_path, modelcheckpoint_path, augmented_images_path, run_name = \
                    create_run_folders(augmentation_d, img_path, prj_path, cfg.trainHistory_subname,
                                       label_name_in)
                # create cache path
                cache_p = os.path.join(cfg.tmp_p, 'cache', run_name, '')
                if cfg.verbose:
                    print('cache', cache_p)
                # ensure it gets reloaded
                if os.path.exists(cache_p):
                    shutil.rmtree(cache_p)
                os.mkdir(cache_p)

                #create some label visualisations
                with open(run_path + 'dfs_in', 'wb') as f:
                    pickle.dump([labels_df, train_df, validation_df, test_df], f)
                for df_n, df in zip(['All', 'Train', 'Validation', 'Test'],
                                    [labels_df, train_df, validation_df, test_df]):
                    for col in df.columns:
                        if col in [label_name, 'reversed', 'transformed', 'normalized', 'label']:
                            add_n = col
                            if col == label_name:
                                add_n = 'Raw Data'
                            elif col == 'label':
                                add_n = 'Input'
                            title = label_name.replace('_', ' ').title() + ' ' + '(n=' + str(len(df)) + ')'
                            visualizations.histogram(df[col], 'probability', title, run_path,
                                                     label_name + df_n + ' ' + add_n)

                # save config
                shutil.copyfile('config.py', run_path + '/config.py')

                # strategy scope from tf.distribute.MirroredStrategy(gpus) (cf. top of file) in TF2 is used for mutlti-gpu
                # usage
                with strategy.scope():
                    # Everything that creates variables should be under the strategy scope.
                    # In general this is only model construction & `compile()`.
                    # Load Model
                    train_ds, validation_ds, test_ds, steps_per_epoch_train, steps_per_epoch_val, steps_per_epoch_test = \
                        dataset_creator(train_df, validation_df, test_df, cfg.type_m, num_labels,
                                        cache_p, **augmentation_d)

                    # steps_per_epoch=False
                    loss = cfg.loss()
                    metrics_l = []
                    for m in cfg.metrics_l:
                        metrics_l.append(m())
                    if cfg.type_m == 'categorical':
                        metrics_l.append(tfa.metrics.F1Score(num_classes=num_labels,
                              average='micro'))
                    model = model_loader(num_labels, cfg.type_m, input_shape, **augmentation_d)
                    if cfg.load_model_weights:
                        print('Loading model from', cfg.load_model_weights)
                        # model = tf.keras.models.load_model(cfg.load_model_weights)
                        model.load_weights(cfg.load_model_weights)
                        print('Predicting with loaded model', model)
                        beta, alpha, pearson_corr, rmse, sk_metrics_d = \
                            create_visualisations(model, test_ds, steps_per_epoch_test, run_path, label_mapping,
                                                  add_params, labels_df, scaler, label_d, add_name='_loded_model')
                        print('returned from create_vis')
                    ###Define Parameters for run
                    optimizer = optimizer_loading()
                    callbacks_l = callback_loader(run_path, modelcheckpoint_path, metrics_l[0])

                    model.compile(optimizer=optimizer, loss=loss, metrics=metrics_l)#, run_eagerly=True)
                    # Load weights
                    #buggy - don't know why (works without by name for same amount of classes/regression
                    # if cfg.load_model_weights:
                    #     model = model.load_weights(cfg.load_model_weights)
                    #     print('loaded weights from', cfg.load_model_weights)
                    if cfg.verbose:
                        print('Final Model', type(model))
                        print(model.summary())

                ###Run Model
                t_begin = time.time()
                print('steps per epoch (train/val/test)', steps_per_epoch_train, steps_per_epoch_val, steps_per_epoch_test)
                try:
                    history = model.fit(
                    train_ds,
                    class_weight=class_weights,
                    validation_data=validation_ds,
                    epochs=cfg.epochs,
                    callbacks=callbacks_l,
                    steps_per_epoch=steps_per_epoch_train,
                    validation_steps=steps_per_epoch_val)
                except KeyboardInterrupt:
                    history = False
                    pass
                fit_time = time.time() - t_begin
                if cfg.verbose:
                    print('Time to fit model (s)', fit_time)

                # Save history as pickle
                if history:
                    with open(os.path.join(run_path, 'trainHistory'), 'wb') as file_pi:
                        pickle.dump(history.history, file_pi)
                # Save model
                model.save(os.path.join(run_path, 'Model'), save_format='h5')

                ###Visualize results
                ###Visualize results
                # save augmented images
                if cfg.save_augmented_images:
                    print('aug', augmentation_d)
                    if augmentation_d:
                        data_augmentation = augmentation(**augmentation_d)
                    for images, _ in train_ds.take(1):
                        for nr, image in enumerate(images):
                            print(nr, image.shape)
                            print(augmentation_d)
                            plt.figure(figsize=(10, 10))
                            for i in range(9):
                                # if augmentation_d:
                                #     img = data_augmentation(image)
                                img = image
                                ax = plt.subplot(3, 3, i + 1)
                                img = img.numpy().astype("float32")
                                img = np.moveaxis(img, 0, -1)
                                img = (img - np.min(img)) / (np.max(img) - np.min(img))
                                # print(img)
                                plt.imshow(img)
                                plt.axis("off")
                            plt.savefig(augmented_images_path + str(nr) + 'unaug')
                            # plt.show()

                            if augmentation_d:
                                plt.figure(figsize=(10, 10))
                                print('augmented image')
                                for i in range(9):
                                    if augmentation_d:
                                        img = data_augmentation(image)
                                    else:
                                        img = image
                                    ax = plt.subplot(3, 3, i + 1)
                                    img = img.numpy().astype("float32")
                                    img = np.moveaxis(img, 0, -1)
                                    img = (img - np.min(img)) / (np.max(img) - np.min(img))
                                    # print(img)
                                    plt.imshow(img)
                                    plt.axis("off")
                                plt.savefig(augmented_images_path + str(nr))
                                # plt.show()
                            if nr >= cfg.save_augmented_images:
                                break
                    if cfg.verbose:
                        print('saved augmented images to', augmented_images_path)

                if cfg.verbose and history:
                    print(history.history.keys())
                    print(history.history)

                #delete cache_p again
                if os.path.exists(cache_p):
                    shutil.rmtree(cache_p)

                # summarize statistics
                additional_reports_d = {}
                if history:
                    additional_reports_d = {'max epoch': len(history.history['val_loss']),
                                            'fit time': fit_time,
                                            'Time per epoch': fit_time / len(history.history['val_loss'])}
                if history:
                    for metric in metrics_l:
                        mn = metric.name
                        if cfg.type_m == 'categorical':
                            additional_reports_d['max val ' + mn] = max(history.history['val_' + mn])
                            additional_reports_d['max val epoch ' + mn] = history.history['val_' + mn].index(max(
                                history.history['val_' + mn]))
                            additional_reports_d['val ' + mn + ' at break'] = history.history['val_' + mn][-1]
                        else:
                            additional_reports_d['min val ' + mn] = min(history.history['val_' + mn])
                            additional_reports_d['min val epoch ' + mn] = history.history['val_' + mn].index(min(
                                history.history['val_' + mn]))
                            additional_reports_d['val ' + mn + ' at break'] = history.history['val_' + mn][-1]
                    # visualize history
                    visualizations.plot_history(history, run_path, cfg.type_m)


                ### Evaluation
                # Evaluate the model via the test dataset
                # (highest validation accuracy seems to always perform best)
                if cfg.reload_best_weights_for_eval:
                    model.load_weights(modelcheckpoint_path)

                if cfg.verbose:
                    print("Evaluating on test data", test_ds)
                test_history = model.evaluate(test_ds, steps=steps_per_epoch_test)
                if cfg.verbose:
                    print("test loss, test", metrics_l[0].name, ':', test_history)
                for nr, r in enumerate(test_history):
                    if nr == 0:
                        additional_reports_d['test_loss'] = r
                    additional_reports_d['test_' + metrics_l[nr - 1].name] = r

                beta, alpha, pearson_corr, rmse, sk_metrics_d = \
                    create_visualisations(model, test_ds, steps_per_epoch_test, run_path, label_mapping, add_params,
                                          labels_df, scaler, label_d)

                # write report (it's a bit messy right now!)
                report_d = {**additional_reports_d, **sk_metrics_d, **{'run_name': run_name}, **labels_amount,
                            **add_params, **label_d}
                filename = os.path.join(train_history_path, 'run_summary_' + label_name_in + '.csv')
                file_exists = os.path.isfile(filename)
                # append to file
                with open(filename, 'a') as csv_file:
                    writer = csv.DictWriter(csv_file, fieldnames=report_d.keys())
                    if not file_exists:
                        writer.writeheader()  # file doesn't exist yet, write a header
                    for k, v in report_d.items():
                        print(k, v)
                    writer.writerow(report_d)

                #delete stuff
                del train_ds, validation_ds, test_ds, model
                print('finished iteration', augmentation_d)


if __name__ == "__main__":
    main()



