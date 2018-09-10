#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time    : 17-9-22 下午1:39
# @Author  : Luo Yao
# @Site    : http://github.com/TJCVRS
# @File    : train_shadownet.py
# @IDE: PyCharm Community Edition
"""
Train shadow net script
"""
import os
import tensorflow as tf
import os.path as ops
import time
import numpy as np
import argparse

from crnn_model import crnn_model
from local_utils import data_utils, log_utils
from global_configuration import config
from local_utils.log_utils import compute_accuracy

logger = log_utils.init_logger()


def init_args() -> argparse.Namespace:
    """
    :return: an object containing all parsed arguments
    """
    parser = argparse.ArgumentParser()
    parser.add_argument('-d', '--dataset_dir', type=str, required=True,
                        help='Path to dir containing train/test data and annotation files.')
    parser.add_argument('-c', '--charset_dir', type=str, default='data/char_dict',
                        help='Path to dir where character sets for the dataset were stored')
    parser.add_argument('-e', '--decode_outputs', action='store_true', default=False,
                        help='Activate decoding of predictions during training (slow!)')
    parser.add_argument('-w', '--weights_path', type=str, help='Path to pre-trained weights.')
    parser.add_argument('-j', '--num_threads', type=int, default=int(os.cpu_count()/2),
                        help='Number of threads to use in batch shuffling')

    return parser.parse_args()


def train_shadownet(dataset_dir: str, charset_dir: str, weights_path: str=None,
                    decode: bool=False, num_threads: int=4):
    """

    :param dataset_dir: Path to Train and Test directories
    :param charset_dir: Path to char_dict.json and ord_map.json (generated with write_text_features.py)
    :param weights_path: Path to stored weights
    :param decode: Whether to perform CTC decoding to report progress during training
    :param num_threads: Number of threads to use in tf.train.shuffle_batch
    """
    # decode the tf records to get the training data
    decoder = data_utils.TextFeatureIO(char_dict_path=ops.join(charset_dir, 'char_dict.json'),
                                       ord_map_dict_path=ops.join(charset_dir, 'ord_map.json')).reader
    images, labels, imagenames = decoder.read_features(ops.join(dataset_dir, 'train_feature.tfrecords'),
                                                       num_epochs=None)
    inputdata, input_labels, input_imagenames = tf.train.shuffle_batch(
        tensors=[images, labels, imagenames], batch_size=config.cfg.TRAIN.BATCH_SIZE,
        capacity=1000 + 2*config.cfg.TRAIN.BATCH_SIZE, min_after_dequeue=100, num_threads=num_threads)

    inputdata = tf.cast(x=inputdata, dtype=tf.float32)

    # initialise the net model
    shadownet = crnn_model.ShadowNet(phase='Train',
                                     hidden_nums=config.cfg.ARCH.HIDDEN_UNITS,
                                     layers_nums=config.cfg.ARCH.HIDDEN_LAYERS,
                                     num_classes=len(decoder.char_dict)+1)

    with tf.variable_scope('shadow', reuse=False):
        net_out = shadownet.build_shadownet(inputdata=inputdata)

    cost = tf.reduce_mean(tf.nn.ctc_loss(labels=input_labels, inputs=net_out,
                                         sequence_length=config.cfg.ARCH.SEQ_LENGTH*np.ones(config.cfg.TRAIN.BATCH_SIZE)))

    decoded, log_prob = tf.nn.ctc_beam_search_decoder(net_out,
                                                      config.cfg.ARCH.SEQ_LENGTH*np.ones(config.cfg.TRAIN.BATCH_SIZE),
                                                      merge_repeated=False)

    sequence_dist = tf.reduce_mean(tf.edit_distance(tf.cast(decoded[0], tf.int32), input_labels))

    global_step = tf.Variable(0, name='global_step', trainable=False)

    starter_learning_rate = config.cfg.TRAIN.LEARNING_RATE
    learning_rate = tf.train.exponential_decay(starter_learning_rate, global_step,
                                               config.cfg.TRAIN.LR_DECAY_STEPS, config.cfg.TRAIN.LR_DECAY_RATE,
                                               staircase=True)
    update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)

    with tf.control_dependencies(update_ops):
        optimizer = tf.train.AdadeltaOptimizer(learning_rate=learning_rate).minimize(loss=cost, global_step=global_step)

    # Set tf summary
    tboard_save_path = config.cfg.PATH.TBOARD_SAVE_PATH
    if not ops.exists(tboard_save_path):
        os.makedirs(tboard_save_path)
    tf.summary.scalar(name='Cost', tensor=cost)
    tf.summary.scalar(name='Learning_Rate', tensor=learning_rate)
    tf.summary.scalar(name='Seq_Dist', tensor=sequence_dist)
    merge_summary_op = tf.summary.merge_all()

    # Set saver configuration
    saver = tf.train.Saver()
    model_save_dir = config.cfg.PATH.MODEL_SAVE_DIR
    if not ops.exists(model_save_dir):
        os.makedirs(model_save_dir)
    train_start_time = time.strftime('%Y-%m-%d-%H-%M-%S', time.localtime(time.time()))
    model_name = 'shadownet_{:s}.ckpt'.format(str(train_start_time))
    model_save_path = ops.join(model_save_dir, model_name)

    # Set sess configuration
    sess_config = tf.ConfigProto()
    sess_config.gpu_options.per_process_gpu_memory_fraction = config.cfg.TRAIN.GPU_MEMORY_FRACTION
    sess_config.gpu_options.allow_growth = config.cfg.TRAIN.TF_ALLOW_GROWTH

    sess = tf.Session(config=sess_config)

    summary_writer = tf.summary.FileWriter(tboard_save_path)
    summary_writer.add_graph(sess.graph)

    # Set the training parameters
    train_epochs = config.cfg.TRAIN.EPOCHS

    with sess.as_default():
        if weights_path is None:
            logger.info('Training from scratch')
            init = tf.global_variables_initializer()
            sess.run(init)
        else:
            logger.info('Restore model from {:s}'.format(weights_path))
            saver.restore(sess=sess, save_path=weights_path)

        coord = tf.train.Coordinator()
        threads = tf.train.start_queue_runners(sess=sess, coord=coord)

        for epoch in range(train_epochs):
            if decode:
                _, c, seq_distance, predictions, labels, summary = sess.run(
                    [optimizer, cost, sequence_dist, decoded, input_labels, merge_summary_op])

                labels = decoder.sparse_tensor_to_str(labels)
                predictions = decoder.sparse_tensor_to_str(predictions[0])
                accuracy = compute_accuracy(labels, predictions)

                if epoch % config.cfg.TRAIN.DISPLAY_STEP == 0:
                    logger.info('Epoch: {:d} cost= {:9f} seq distance= {:9f} train accuracy= {:9f}'.format(
                        epoch + 1, c, seq_distance, accuracy))

            else:
                _, c, summary = sess.run([optimizer, cost, merge_summary_op])
                if epoch % config.cfg.TRAIN.DISPLAY_STEP == 0:
                    logger.info('Epoch: {:d} cost= {:9f}'.format(epoch + 1, c))

            summary_writer.add_summary(summary=summary, global_step=epoch)
            saver.save(sess=sess, save_path=model_save_path, global_step=epoch)

        coord.request_stop()
        coord.join(threads=threads)


if __name__ == '__main__':
    args = init_args()

    if not ops.exists(args.dataset_dir):
        raise ValueError('{:s} doesn\'t exist'.format(args.dataset_dir))

    train_shadownet(dataset_dir=args.dataset_dir, charset_dir=args.charset_dir,
                    weights_path=args.weights_path, decode=args.decode_outputs, num_threads=args.num_threads)
    print('Done')
