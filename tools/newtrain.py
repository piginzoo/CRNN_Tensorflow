#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Train Script
"""
import os
import tensorflow as tf
import time
import numpy as np
import datetime
from crnn_model import crnn_model
from local_utils import data_utils, log_utils
from config import config
from utils import tensor_util
from utils import image_util
from utils import text_util
from utils.data_factory import DataFactory

tf.app.flags.DEFINE_string('name', 'CRNN', 'no use ,just a flag for shell batch')
tf.app.flags.DEFINE_boolean('debug', False, 'debug mode')
tf.app.flags.DEFINE_string('train_dir','data/train','')
tf.app.flags.DEFINE_string('label_file','train.txt','')
tf.app.flags.DEFINE_string('charset','','')
tf.app.flags.DEFINE_string('tboard_dir', 'tboard', 'tboard data dir')
tf.app.flags.DEFINE_string('weights_path', None, 'model path')
tf.app.flags.DEFINE_integer('validate_steps', 10, 'model path')
tf.app.flags.DEFINE_string('validate_file','data/test.txt','')
tf.app.flags.DEFINE_integer('num_threads', 4, 'read train data threads')
tf.app.flags.DEFINE_string('resize_mode', 'resize_force', 'image resize mode')
FLAGS = tf.app.flags.FLAGS

logger = log_utils.init_logger()


def save_model(saver,sess,epoch):
    model_save_dir = 'model'
    if not os.path.exists(model_save_dir):
        os.makedirs(model_save_dir)
    train_start_time = time.strftime('%Y-%m-%d-%H-%M-%S', time.localtime(time.time()))
    model_name = 'crnn_{:s}.ckpt'.format(str(train_start_time))
    model_save_path = os.path.join(model_save_dir, model_name)
    saver.save(sess=sess, save_path=model_save_path, global_step=epoch)
    logger.info("训练: 保存了模型：%s", model_save_path)


def create_summary_writer(sess):
    # 按照日期，一天生成一个Summary/Tboard数据目录
    # Set tf summary
    if not os.path.exists(FLAGS.tboard_dir): os.makedirs(FLAGS.tboard_dir)
    today = datetime.datetime.now().strftime("%Y%m%d-%H%M")
    summary_dir = os.path.join(FLAGS.tboard_dir,today)
    summary_writer = tf.summary.FileWriter(summary_dir)
    summary_writer.add_graph(sess.graph)
    return summary_writer


def train(weights_path=None):
    logger.info("开始训练")

    # 获取字符库
    characters = text_util.get_charset(FLAGS.charset)

    # 定义张量
    input_image = tf.placeholder(tf.float32, shape=[None, 32, None, 3], name='input_image')
    sparse_label = tf.sparse_placeholder(tf.int32)
    # input_label = tf.placeholder(tf.int32,  shape=[None, ], name='input_label')
    sequence_size = tf.placeholder(tf.int32, shape=[None])

    # 创建模型
    network = crnn_model.ShadowNet(phase='Train',
                                     hidden_nums=config.cfg.ARCH.HIDDEN_UNITS, # 256
                                     layers_nums=config.cfg.ARCH.HIDDEN_LAYERS,# 2层
                                     num_classes=len(characters) + 1)

    with tf.variable_scope('shadow', reuse=False):
        net_out = network.build(inputdata=input_image, sequence_len=sequence_size)

    #logger.debug("222222.")
    # 将tensor转换为稀疏矩阵
    # sparse_label = tensor_util.convert_to_sparse_tensor(input_label)
    # sparse_label = input_label
    #logger.debug("input lable convert_to_sparse_tensor success.")

    # 创建优化器和损失函数的op
    cost, optimizer, global_step = network.loss(net_out, sparse_label, sequence_size)

    # 创建校验用的decode和编辑距离
    validate_decode, sequence_dist = network.validate(net_out, sparse_label, sequence_size)

    # 创建一个变量用于把计算的精确度加载到summary中
    accuracy = tf.Variable(0, name='accuracy', trainable=False)
    tf.summary.scalar(name='validate.Accuracy', tensor=accuracy)

    train_summary_op = tf.summary.merge_all(scope="train")
    validate_summary_op = tf.summary.merge_all(scope="validate")
    # _p_shape(train_summary_op,"训练阶段的Summary收集")
    # _p_shape(train_summary_op,"校验阶段的Summary收集")

    # Set saver configuration
    saver = tf.train.Saver()

    # Set sess configuration
    sess_config = tf.ConfigProto()
    sess_config.gpu_options.per_process_gpu_memory_fraction = config.cfg.TRAIN.GPU_MEMORY_FRACTION
    sess_config.gpu_options.allow_growth = config.cfg.TRAIN.TF_ALLOW_GROWTH

    sess = tf.Session(config=sess_config)
    logger.debug("创建session")

    summary_writer = create_summary_writer(sess)

    # Set the training parameters
    train_epochs = config.cfg.TRAIN.EPOCHS

    with sess.as_default():

        sess.run(tf.local_variables_initializer())
        if weights_path is None:
            logger.info('从头开始训练，不加载旧模型')
            init = tf.global_variables_initializer()
            sess.run(init)
        else:
            logger.info('从文件{:s}恢复模型，继续训练'.format(weights_path))
            saver.restore(sess=sess, save_path=weights_path)

        data_generator = DataFactory.get_batch(data_dir=FLAGS.train_dir,
                                               charsets=characters,
                                               data_type='train',
                                               batch_size=config.cfg.TRAIN.BATCH_SIZE,
                                               num_workers=FLAGS.num_threads)
        for epoch in range(1, train_epochs+1):
            logger.debug("训练: 第%d次", epoch)

            # 获取数据
            data = next(data_generator)
            # Image缩放处理
            data_image = image_util.resize_batch_image(data[0], FLAGS.resize_mode, config.cfg.ARCH.INPUT_SIZE)
            # logger.debug("data_image.shape = %r", data_image.shape)
            # Image序列宽度
            data_seq = [(img.shape[1] // config.cfg.ARCH.WIDTH_REDUCE_TIMES) for img in data_image]
            # Label扩展处理
            # data_label = text_util.extend_to_max_len(data[1])
            # logger.debug("data_label.shape = %r", data_label.shape)
            data_label = tensor_util.to_sparse_tensor(data[1])

            # validate一下
            if epoch % FLAGS.validate_steps == 0:
                logger.info('此Epoch为检验(validate)')
                # 梯度下降，并且采集各种数据：编辑距离、预测结果、输入结果、训练summary和校验summary
                # 这过程非常慢，32batch的实测在K40的显卡上，实测需要15分钟
                seq_distance,preds,labels_sparse,v_summary = sess.run(
                    [sequence_dist, validate_decode, sparse_label, validate_summary_op],
                    feed_dict={ input_image:data_image,
                                # input_label: data_label,
                                sparse_label:tf.SparseTensorValue(data_label[0], data_label[1], data_label[2]),
                                sequence_size: data_seq })
                logger.info(': Epoch: {:d} session.run结束'.format(epoch))

                _accuracy = data_utils.caculate_accuracy(preds, labels_sparse,characters)
                tf.assign(accuracy, _accuracy) # 更新正确率变量
                logger.info('正确率计算完毕：%f', _accuracy)

                summary_writer.add_summary(summary=v_summary, global_step=epoch)
                logger.debug("写入校验、距离计算、正确率Summary")

            # 单纯训练
            else:
                _, ctc_lost, t_summary = sess.run([optimizer, cost, train_summary_op],
                    feed_dict={ input_image:data_image,
                                # input_label: data_label,
                                sparse_label:tf.SparseTensorValue(data_label[0], data_label[1], data_label[2]),
                                sequence_size: data_seq })

                logger.debug("训练: 优化完成、cost计算完成、Summary写入完成")
                summary_writer.add_summary(summary=t_summary, global_step=epoch)
                logger.debug("写入训练Summary")


            # 10万个样本，一个epoch是3.5分钟，CHECKPOINT_STEP=20，大约是70分钟存一次
            if epoch % config.cfg.TRAIN.CHECKPOINT_STEP == 0:
                save_model(saver,sess,epoch)

    sess.close()




if __name__ == '__main__':
    print("开始训练...")
    train(FLAGS.weights_path)

