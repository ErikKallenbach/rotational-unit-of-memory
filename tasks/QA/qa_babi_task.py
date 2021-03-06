from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import numpy as np
import argparse
import os
import tensorflow as tf
import sys
import tarfile
import re
import errno
import random
import datetime

from utils import *

from tensorflow.contrib.rnn import BasicLSTMCell, BasicRNNCell, GRUCell
from RUM import RUMCell, rotate
from baselineModels.GORU import GORUCell
from baselineModels.EUNN import EUNNCell

sp = None  # global bool variable for the single_pass option
g = None  # global variable in which we will write the save_path for the single_pass


# preprocess data


def tokenize(sent):
    '''Return the tokens of a sentence including punctuation.

    >>> tokenize('Bob dropped the apple. Where is the apple?')
    ['Bob', 'dropped', 'the', 'apple', '.', 'Where', 'is', 'the', 'apple', '?']
    '''
    return [x.strip() for x in re.split('(\W+)?', sent) if x.strip()]


def parse_stories(lines, only_supporting=False):
    '''Parse stories provided in the bAbi tasks format

    If only_supporting is true,
    only the sentences that support the answer are kept.
    '''
    data = []
    story = []
    for line in lines:
        line = line.decode('utf-8').strip()
        nid, line = line.split(' ', 1)
        nid = int(nid)
        if nid == 1:
            story = []
        if '\t' in line:
            q, a, supporting = line.split('\t')
            q = tokenize(q)
            substory = None
            if only_supporting:
                # Only select the related substory
                supporting = map(int, supporting.split())
                substory = [story[i - 1] for i in supporting]
            else:
                # Provide all the substories
                substory = [x for x in story if x]
            data.append((substory, q, a))
            story.append('')
        else:
            sent = tokenize(line)
            story.append(sent)
    return data


def get_stories(level, f, only_supporting=False, max_length=None):
    '''Given a file name, read the file, retrieve the stories,
    and then convert the sentences into a single story.

    If max_length is supplied,
    any stories longer than max_length tokens will be discarded.
    '''
    data = parse_stories(f.readlines(), only_supporting=only_supporting)
    if level == "word":
        # word level needs to be more granular
        flatten = lambda data: reduce(lambda x, y: x + y, data)
        data = [(flatten(story), q, answer) for story, q,
                answer in data if not max_length or len(flatten(story)) < max_length]

    return data


def vectorize_stories(data, word_idx, story_maxlen, query_maxlen, attention, level):
    """ vectorizes the stories.
        there are two levels to consider: word and sentence.
    """
    xs = []
    qs = []
    ys = []
    x_len = []
    q_len = []
    vocab_length = len(word_idx) + 1

    # for sentence level
    def one_hot(ind):
        """ one hot helper function """
        return np.array([(i == ind) for i in range(vocab_length)], dtype=float)

    # main loop
    for story, query, answer in data:
        if level == "word":
            x = [word_idx[w] for w in story]
            q = [word_idx[w] for w in query]
            len_x = len(x)
            if len_x < story_maxlen:
                x = [0] * (story_maxlen - len_x) + x
            len_q = len(q)
            q_len.append(len_q)
            for i in range(len_q, query_maxlen):
                q.append(0)
            y = word_idx[answer]
            x_len.append(len_x)
            xs.append(x)
            qs.append(q)
            ys.append(y)
        elif level == "sentence":
            len_story = len(story)
            x = [sum([one_hot(word_idx[w])
                      for w in sentence]) for sentence in story]
            len_x = len(x)
            x_len.append(len_x)
            for i in range(story_maxlen - len_x):
                x = [one_hot(0)] + x
            q = sum([one_hot(word_idx[w]) for w in query])
            xs.append(x)
            qs.append([q])
            ys.append(word_idx[answer])
        else:
            raise ValueError(
                "Level must be either 'word' or 'sentence'.")

    if level == "sentence":
        q_len = None
    return np.array(xs), np.array(qs), np.array(ys), x_len, q_len


def word_model(cell,
               n_hidden,
               n_embed,
               attention,
               vocab_size,
               story_maxlen,
               query_maxlen
               ):
    """ defining the NN core for the word level.
        this code is deprecated (needs further resarch).
    """

    # model
    sentence = tf.placeholder("int32", [None, story_maxlen])
    question = tf.placeholder("int32", [None, query_maxlen])
    answer_holder = tf.placeholder("int64", [None])

    n_output = n_hidden
    n_input = n_embed
    n_classes = vocab_size

    with tf.variable_scope("embedding"):
        embed_init_val = np.sqrt(6.) / np.sqrt(vocab_size)
        embed = tf.get_variable("embedding", [vocab_size, n_embed],
                                initializer=tf.random_normal_initializer(
            -embed_init_val, embed_init_val), dtype=tf.float32)
        encoded_story = tf.nn.embedding_lookup(embed, sentence)
        encoded_question = tf.nn.embedding_lookup(embed, question)
    merged = tf.concat([encoded_story, encoded_question], axis=1)

    merged, _ = tf.nn.dynamic_rnn(cell, merged, dtype=tf.float32)
    print("merged:", col(merged, 'g'))
    # hidden to output
    with tf.variable_scope("hidden_to_output"):
        V_init_val = np.sqrt(6.) / np.sqrt(n_output + n_input)
        V_weights = tf.get_variable("V_weights", shape=[
            n_hidden, n_classes], dtype=tf.float32,
            initializer=tf.random_uniform_initializer(-V_init_val, V_init_val))
        V_bias = tf.get_variable("V_bias", shape=[
            n_classes], dtype=tf.float32, initializer=tf.constant_initializer(0.01))

        merged_list = tf.unstack(merged, axis=1)[-1]
        temp_out = tf.matmul(merged_list, V_weights)
        final_out = tf.nn.bias_add(temp_out, V_bias)

    with tf.variable_scope("loss"):
        # evaluate process
        cost = tf.reduce_mean(tf.nn.sparse_softmax_cross_entropy_with_logits(
            logits=final_out, labels=answer_holder))
        correct_pred = tf.equal(tf.argmax(final_out, 1), answer_holder)
        accuracy = tf.reduce_mean(tf.cast(correct_pred, tf.float32))
    return cost, accuracy, sentence, question, answer_holder


def attention_sentence(rnn_outputs,
                       n_hidden,
                       n_embed,
                       story_maxlen,
                       attn_rum,
                       encoded_question,
                       eps=1e-12,):
    """ the attention mechansim for the 'sentence' level """

    with tf.variable_scope("attention"):
        hidden_question = rnn_outputs[:, -1, :]
        rnn_out = rnn_outputs[:, :-1, :]
        hid_q_tmp = tf.expand_dims(hidden_question, 1)
        energy = tf.reduce_sum(rnn_out * hid_q_tmp, axis=2)
        alphas = tf.nn.softmax(energy, axis=1)
        alphas = tf.expand_dims(alphas, -1)

        weighted_outputs = alphas * rnn_out
        context = tf.reduce_sum(weighted_outputs, axis=1)

        if attn_rum:
            embed_init_val = np.sqrt(6.) / np.sqrt(n_embed + n_hidden)
            embed = tf.get_variable('embed_q', [n_embed, n_hidden],
                                    initializer=tf.random_normal_initializer(
                -embed_init_val, embed_init_val), dtype=tf.float32)
            tmp_enc_q = tf.reshape(encoded_question, [-1, n_embed])
            tmp_matmul = tf.matmul(tmp_enc_q, embed)
            embedded_q = tf.reshape(tmp_matmul, [-1, n_hidden])
            final_h = rotate(embedded_q, context, hidden_question)
            n_hidden_output = n_hidden

        final_h = tf.concat([context, hidden_question], axis=1)
        n_hidden_output = 2 * n_hidden

    return final_h, n_hidden_output


def sentence_model(cell,
                   n_hidden,
                   n_embed,
                   attention,
                   vocab_size,
                   story_maxlen,
                   attn_rum):
    """ defining the NN core for the sentence level.
        this code is deprecated (needs further resarch).
    """
    n_output = n_hidden
    n_input = n_embed
    n_classes = vocab_size

    with tf.variable_scope("embedding"):
        input_story = tf.placeholder(
            "float32", [None, story_maxlen, vocab_size])
        embed_init_val = np.sqrt(6.) / np.sqrt(vocab_size)
        embed = tf.get_variable('embed', [vocab_size, n_embed],
                                initializer=tf.random_normal_initializer(
            -embed_init_val, embed_init_val), dtype=tf.float32)

        tmp_inp = tf.reshape(input_story, [-1, vocab_size])
        tmp_inp = tf.matmul(tmp_inp, embed)
        encoded_story = tf.reshape(tmp_inp, [-1, story_maxlen, n_embed])

        question = tf.placeholder("float32", [None, 1, vocab_size])

        tmp_q = tf.reshape(question, [-1, vocab_size])
        tmp_q = tf.matmul(tmp_q, embed)
        encoded_question = tf.reshape(tmp_q, [-1, 1, n_embed])

        rnn_input = tf.concat([encoded_story, encoded_question], axis=1)

    # unrolls the rnn
    rnn_outputs, _ = tf.nn.dynamic_rnn(
        cell, rnn_input, dtype=tf.float32)

    # gets the output vector
    if not attention:
        final_h = rnn_outputs[:, -1, :]
        n_hidden_output = n_hidden
    else:
        # attention mechanism
        final_h, n_hidden_output = attention_sentence(
            rnn_outputs, n_hidden, n_embed, story_maxlen, attn_rum, encoded_question)

    # hidden layer to output
    V_init_val = np.sqrt(6.) / np.sqrt(n_hidden_output + n_input)
    V_weights = tf.get_variable("V_weights", shape=[
        n_hidden_output, n_classes], dtype=tf.float32,
        initializer=tf.random_uniform_initializer(-V_init_val, V_init_val))
    V_bias = tf.get_variable("V_bias", shape=[
        n_classes], dtype=tf.float32, initializer=tf.constant_initializer(0.01))

    temp_out = tf.matmul(final_h, V_weights)
    final_out = tf.nn.bias_add(temp_out, V_bias)

    answer_holder = tf.placeholder("int64", [None])

    # evaluate process
    cost = tf.reduce_mean(tf.nn.sparse_softmax_cross_entropy_with_logits(
        logits=final_out, labels=answer_holder))
    tf.summary.scalar('cost', cost)
    correct_pred = tf.equal(tf.argmax(final_out, 1), answer_holder)
    accuracy = tf.reduce_mean(tf.cast(correct_pred, tf.float32))
    tf.summary.scalar('accuracy', accuracy)

    return cost, accuracy, input_story, question, answer_holder


def nn_model(cell,
             level,
             attention,
             n_hidden,
             n_embed,
             vocab_size,
             story_maxlen,
             query_maxlen,
             attn_rum):
    """ constructs the core NN model """

    if level == "word":
        cost, accuracy, input_story, question, answer_holder = word_model(cell, n_hidden, n_embed,
                                                                          vocab_size, story_maxlen, query_maxlen)
    elif level == "sentence":
        cost, accuracy, input_story, question, \
            answer_holder = sentence_model(
                cell, n_hidden, n_embed, attention, vocab_size, story_maxlen, attn_rum)
    else:
        raise
    return cost, accuracy, input_story, question, answer_holder


def main(model,
         qid,
         data_path,
         level,
         attention,
         n_iter,
         n_batch,
         n_hidden,
         n_embed,
         capacity,
         comp,
         FFT,
         learning_rate,
         norm,
         update_gate,
         activation,
         lambd,
         layer_norm,
         zoneout,
         attn_rum):
    """ assembles the model, trains and then evaluates. """

    # preprocessing
    learning_rate = float(learning_rate)
    tar = tarfile.open(data_path)
    name_str = [
        'single-supporting-fact',
        'two-supporting-facts',
        'three-supporting-facts',
        'two-arg-relations',
        'three-arg-relations',
        'yes-no-questions',
        'counting',
        'lists-sets',
        'simple-negation',
        'indefinite-knowledge',
        'basic-coreference',
        'conjunction',
        'compound-coreference',
        'time-reasoning',
        'basic-deduction',
        'basic-induction',
        'positional-reasoning',
        'size-reasoning',
        'path-finding',
        'agents-motivations',
    ]
    challenge = 'tasks_1-20_v1-2/en-10k/qa' + \
        str(qid) + '_' + name_str[qid - 1] + '_{}.txt'
    train = get_stories(level, tar.extractfile(challenge.format('train')))
    test = get_stories(level, tar.extractfile(challenge.format('test')))

    # gets vocabulary
    vocab = set()
    for story, q, answer in train + test:
        if level == "word":
            vocab |= set(story + q + [answer])
        elif level == "sentence":
            vocab |= set(
                [item for sublist in story for item in sublist] + q + [answer])
        else:
            raise
    vocab = sorted(vocab)

    # Reserve 0 for masking via pad_sequences
    vocab_size = len(vocab) + 1
    word_idx = dict((c, i + 1) for i, c in enumerate(vocab))

    story_maxlen = max(map(len, (x for x, _, _ in train + test)))
    query_maxlen = max(map(len, (x for _, x, _ in train + test))
                       ) if level == "word" else None

    train_x, train_q, train_y, train_x_len, train_q_len = vectorize_stories(
        train, word_idx, story_maxlen, query_maxlen, attention, level)
    test_x, test_q, test_y, test_x_len, test_q_len = vectorize_stories(
        test, word_idx, story_maxlen, query_maxlen, attention, level)
    # notes: query_maxlen will be `None` if `level == sentence`;
    # moreover we added the `attention` and `level` arguments.

    # number of data points
    n_data = len(train_x)
    n_val = int(0.1 * n_data)
    # val data
    val_x = train_x[-n_val:]
    val_q = train_q[-n_val:]
    val_y = train_y[-n_val:]
    val_x_len = train_x_len[-n_val:]
    val_q_len = train_q_len[-n_val:] if level == "word" else None
    # train data
    train_x = train_x[:-n_val]
    train_q = train_q[:-n_val]
    train_y = train_y[:-n_val]
    train_q_len = train_q_len[:-n_val] if level == "word" else None
    train_x_len = train_x_len[:-n_val]
    n_train = len(train_x)

    # profiler printing
    print(col('level: ' + level, 'y'))
    print(col('attention: ' + str(attention), 'y'))
    print(col('qid: ' + str(qid), 'y'))
    print(col('vocab = {}'.format(vocab), 'y'))
    print(col('x.shape = {}'.format(np.array(train_x).shape), 'y'))
    print(col('xq.shape = {}'.format(np.array(train_q).shape), 'y'))
    print(col('y.shape = {}'.format(np.array(train_y).shape), 'y'))
    print(col('story_maxlen, query_maxlen = {}, {}'.format(
        story_maxlen, query_maxlen), 'y'))
    print(col("building model", "b"))

    # defines the rnn cell
    if model == "LSTM":
        cell = BasicLSTMCell(n_hidden, state_is_tuple=True, forget_bias=1)
    elif model == "GRU":
        cell = GRUCell(n_hidden)
    elif model == "RUM":
        if activation == "relu":
            act = tf.nn.relu
        elif activation == "sigmoid":
            act = tf.nn.sigmoid
        elif activation == "tanh":
            act = tf.nn.tanh
        elif activation == "softsign":
            act = tf.nn.softsign
        cell = RUMCell(n_hidden,
                       eta_=norm,
                       update_gate=update_gate,
                       lambda_=lambd,
                       activation=act,
                       use_layer_norm=layer_norm,
                       use_zoneout=zoneout)
    elif model == "EUNN":
        cell = EUNNCell(n_hidden, capacity, FFT, comp, name="eunn")
    elif model == "GORU":
        cell = GORUCell(n_hidden, capacity, FFT)
    elif model == "RNN":
        cell = BasicRNNCell(n_hidden)

    cost, accuracy, input_story, question, answer_holder = nn_model(cell,
                                                                    level,
                                                                    attention,
                                                                    n_hidden,
                                                                    n_embed,
                                                                    vocab_size,
                                                                    story_maxlen,
                                                                    query_maxlen,
                                                                    attn_rum)

    # initialization
    tf.summary.scalar('cost', cost)
    if not (level == "word" and attention):
        tf.summary.scalar('accuracy', accuracy)
    optimizer = tf.train.AdamOptimizer(
        learning_rate=learning_rate).minimize(cost)
    init = tf.global_variables_initializer()

    # save
    filename = ("attn" if attention else "") + \
        model + "_H" + str(n_hidden) + "_" + \
        ("L" + str(lambd) + "_" if lambd else "") + \
        ("E" + str(norm) + "_" if norm else "") + \
        ("A" + activation + "_" if activation else "") + \
        ("U_" if update_gate and model == "RUM" else "") + \
        ("Z_" if zoneout and model == "RUM" else "") + \
        ("RA_" if attn_rum and model == "RUM" else "") + \
        ("ln_" if layer_norm and model == "RUM" else "") + \
        (str(capacity) if model in ["EUNN", "GORU"] else "") + \
        ("FFT_" if model in ["EUNN", "GORU"] and FFT else "") + \
        ("NE" + str(n_embed) + "_") + \
        "B" + str(n_batch)
    save_dir = os.path.join('../../train_log', 'babi', level)
    save_path = os.path.join(save_dir, str(qid), filename)

    print(col("file managing: " + save_path, "b"))
    file_manager(save_path)

    # what follows is task specific
    filepath = os.path.join(save_path, "eval.txt")
    if not os.path.exists(os.path.dirname(filepath)):
        try:
            os.makedirs(os.path.dirname(filepath))
        except OSError as exc:
            if exc.errno != errno.EEXIST:
                raise
    f = open(filepath, 'w')
    f.write("validation\n")

    log(kwargs, save_path)

    # training loop
    merged_summary = tf.summary.merge_all()
    saver = tf.train.Saver()
    parameters_profiler()

    # early stop
    ultimate_accuracy = -1.0
    ultimate_steps = 0  # if 5 steps with no improvement, we should stop training

    step = 0
    with tf.Session(config=tf.ConfigProto(log_device_placement=False,
                                          allow_soft_placement=False)) as sess:

        print(col("saving summary data in " + save_path, "b"))
        train_writer = tf.summary.FileWriter(save_path, sess.graph)
        sess.run(init)

        steps = []
        losses = []
        accs = []

        # prepare validation/test dictinary
        # validation
        val_dict = {input_story: val_x,
                    question: val_q, answer_holder: val_y}
        # test
        test_dict = {input_story: test_x,
                     question: test_q, answer_holder: test_y}

        # the factor of 10 is tentative [experimental]
        while step < 10 * n_iter:
            a = int(step % (n_train / n_batch))
            batch_x = train_x[a * n_batch: (a + 1) * n_batch]
            batch_q = train_q[a * n_batch: (a + 1) * n_batch]
            batch_y = train_y[a * n_batch: (a + 1) * n_batch]

            train_dict = {input_story: batch_x,
                          question: batch_q, answer_holder: batch_y}
            summ, loss = sess.run(
                [merged_summary, cost], feed_dict=train_dict)
            train_writer.add_summary(summ, step)
            sess.run(optimizer, feed_dict=train_dict)

            if not (level == "word" and attention):
                acc = sess.run(accuracy, feed_dict=train_dict)
                if step % 100 == 0:
                    print(col("Iter " + str(step) + ", Minibatch Loss= " +
                              "{:.6f}".format(loss) + ", Training Accuracy= " +
                              "{:.5f}".format(acc), 'g'))
            else:
                if step % 100 == 0:
                    print(col("Iter " + str(step) + ", Minibatch Loss= " +
                              "{:.6f}".format(loss), 'g'))
            steps.append(step)
            losses.append(loss)
            if not (level == "word" and attention):
                accs.append(acc)
            step += 1

            if step % 500 == 1:
                val_loss, val_acc = sess.run(
                    [cost, accuracy], feed_dict=val_dict)
                print(col("Validation Loss= " +
                          "{:.6f}".format(val_loss) + ", Validation Accuracy= " +
                          "{:.5f}".format(val_acc), "g"))
                if val_acc > ultimate_accuracy:
                    ultimate_accuracy = val_acc
                    print(col("saving graph and metadata in " + save_path, "b"))
                    saver.save(sess, os.path.join(save_path, "model"))
                    ultimate_steps = 0
                else:
                    ultimate_steps += 1
                if ultimate_steps == 10:
                    print(col("Early stop!", 'r'))
                    break
                print(col((ultimate_accuracy, ultimate_steps), 'r'))

        print(col("Optimization Finished!", 'b'))

        # test
        print(col("restoring from " + save_path + "/model", "b"))
        saver.restore(sess, save_path + "/model")
        print(col("restored the best model on the validation data", "b"))
        test_acc, test_loss = sess.run(
            [accuracy, cost], feed_dict=test_dict)
        f.write("Test result: Loss= " + "{:.6f}".format(test_loss) +
                ", Accuracy= " + "{:.5f}\n".format(test_acc))
        print(col("Test result: Loss= " + "{:.6f}".format(test_loss) +
                  ", Accuracy= " + "{:.5f}".format(test_acc), "g"))
        f.close()

    # what follow is for the single pass
    global sp
    global g
    if sp:
        single_pass_path = os.path.join(
            save_dir, "summary_eval_" + filename + ".txt")
        if g == None:
            g = open(single_pass_path, 'w')
            if not os.path.exists(single_pass_path):
                try:
                    os.makedirs(os.path.dirname(single_pass_path))
                except OSError as exc:
                    if exc.errno != errno.EEXIST:
                        raise
            g.write(col(datetime.datetime.now().strftime(
                "%Y-%m-%d %H:%M:%S") + "\n", 'r'))
        g.write(col("id " + str(qid) + ": " +
                    "{:.5f}".format(test_acc) + "\n", "y"))
        g.flush()
    return test_acc  # returns the test accuracy to calculate the average accuracy

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="babi task")
    parser.add_argument('--gpu', help='comma separated list of GPU(s) to use.')
    parser.add_argument("model", default='LSTM',
                        help='Model name: LSTM, EUNN, GRU, GORU')
    parser.add_argument('qid', type=int, default=-1, help='Test set')
    parser.add_argument('level', type=str, default="sentence",
                        help='level: word or sentence')
    parser.add_argument('attention', type=str,
                        default="False", help='is attn. mechn.')
    parser.add_argument('--n_iter', '-I', type=int,
                        default=10000, help='training iteration number')
    parser.add_argument(
        '--data_path', default="../../data/babi/tasks_1-20_v1-2.tar", type=str)
    parser.add_argument('--n_batch', '-B', type=int,
                        default=32, help='batch size')
    parser.add_argument('--n_hidden', '-H', type=int,
                        default=256, help='hidden layer size')
    parser.add_argument('--n_embed', '-E', type=int,
                        default=64, help='embedding size')
    parser.add_argument('--capacity', '-L', type=int, default=2,
                        help='Tunable style capacity, only for EUNN, default value is 2')
    parser.add_argument('--comp', '-C', type=str, default="False",
                        help='Complex domain or Real domain. Default is False: real domain')
    parser.add_argument('--FFT', '-F', type=str, default="False",
                        help='FFT style, default is False')
    parser.add_argument('--learning_rate', '-R', default=0.001, type=str)
    parser.add_argument('--norm', '-N', default=None, type=float)
    parser.add_argument('--update_gate', '-U', default="True",
                        type=str, help='is there update gate?')
    parser.add_argument('--activation', '-A', default="relu",
                        type=str, help='specify activation')
    parser.add_argument('--lambd', '-LA', default=0,
                        type=int, help='lambda for RUM model')
    parser.add_argument('--layer_norm', '-LN', default="False",
                        type=str, help='is there layer normalization?')
    parser.add_argument('--zoneout', '-Z', default="False",
                        type=str, help='is there zoneout?')
    parser.add_argument('--single_pass', default=0,
                        type=bool, help='single pass evaluation?')
    parser.add_argument('--attn_rum', '-RA', default="False",
                        type=str, help='attention RUM?')

    args = parser.parse_args()
    dicts = vars(args)

    if args.gpu:
        os.environ['CUDA_VISIBLE_DEVICES'] = args.gpu

    for i in dicts:
        if (dicts[i] == "False"):
            dicts[i] = False
        elif dicts[i] == "True":
            dicts[i] = True

    kwargs = {
        'model': dicts['model'],
        'qid': dicts['qid'],
        'level': dicts['level'],
        'attention': dicts['attention'],
        'data_path': dicts['data_path'],
        'n_iter': dicts['n_iter'],
        'n_batch': dicts['n_batch'],
        'n_hidden': dicts['n_hidden'],
        'n_embed': dicts['n_embed'],
        'capacity': dicts['capacity'],
        'comp': dicts['comp'],
        'FFT': dicts['FFT'],
        'learning_rate': dicts['learning_rate'],
        'norm': dicts['norm'],
        'update_gate': dicts['update_gate'],
        'activation': dicts['activation'],
        'lambd': dicts['lambd'],
        'layer_norm': dicts['layer_norm'],
        'zoneout': dicts['zoneout'],
        'attn_rum': dicts['attn_rum']
    }
    sp = args.single_pass

    if args.single_pass:
        assert args.qid == -1
        print(col('starting single pass evaluation', 'b'))
        summ = 0.
        for i in range(1, 21):
            tf.reset_default_graph()
            kwargs['qid'] = i
            summ += main(**kwargs)
        summ = summ / 20.
        g.write(col("average: " + "{:.5f}".format(summ) + "\n", "g"))
        g.close()

    else:
        main(**kwargs)
