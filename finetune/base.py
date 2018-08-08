import os
import random
import warnings
import logging
import pickle
import json
import itertools
from pathlib import Path

import tqdm

from abc import ABCMeta, abstractmethod
from collections import namedtuple, defaultdict
from functools import partial
from copy import deepcopy

import numpy as np
import tensorflow as tf

from finetune.download import download_data_if_required
from finetune.optimizers import AdamWeightDecay, schedules
from sklearn.model_selection import train_test_split

from finetune.network_modules import featurizer, language_model
from finetune.utils import (
    find_trainable_variables, assign_to_gpu, average_grads, interpolate_pos_embed,
    iter_data, soft_split, concat_or_stack,
    guarantee_initialized_variables, sample_with_temperature, list_transpose
)
from finetune.encoding import TextEncoder, ArrayEncodedOutput
from finetune.config import PAD_TOKEN, get_default_config, Ranged

SHAPES_PATH = os.path.join(os.path.dirname(__file__), 'model', 'params_shapes.json')
PARAM_PATH = os.path.join(os.path.dirname(__file__), 'model', 'params_{}.npy')

_LOGGER = logging.getLogger(__name__)

DROPOUT_ON = 1
DROPOUT_OFF = 0
SAVE_PREFIX = 'model'


class BaseModel(object, metaclass=ABCMeta):
    """
    A sklearn-style class for finetuning a Transformer language model on a classification task.
    """

    def __init__(self, config=None, **kwargs):
        """ 
        For a full list of configuration options, see `finetune.config`.
        
        :param config: A config object generated by `finetune.config.get_config` or None (for default config).
        :param **kwargs: key-value pairs of config items to override.
        """

        self.config = config or get_default_config()
        self.config.update(kwargs)
        self.label_encoder = None
        self._initialize()
        self.target_dim = None
        self._load_from_file = False

    def _initialize(self):
        # Initializes the non-serialized bits of the class.
        self._set_random_seed(self.config.seed)

        download_data_if_required()
        self.encoder = TextEncoder()

        # symbolic ops
        self.logits = None  # classification logits
        self.target_loss = None  # cross-entropy loss
        self.lm_losses = None  # language modeling losses
        self.lm_predict_op = None
        self.train = None  # gradient + parameter update
        self.features = None  # hidden representation fed to classifier
        self.summaries = None  # Tensorboard summaries
        self.train_writer = None
        self.valid_writer = None
        self.predict_params = None
        self.train_op = None
        self.predict_op = None
        self.predict_proba_op = None
        self.sess = None
        self.noop = tf.no_op()

        # indicator vars
        self.is_built = False  # has tf graph been constructed?
        self.is_trained = False  # has model been fine-tuned?
        self.require_lm = False

    def _format_for_encoding(self, *Xs):
        """
        Most subclasses take in inputs as:
            List (batch) of list (docs)
        
        Encode_multi_input expect the following format:
            List (batch) of list (docs) of list (subseqs) of text
        
        This method is responsible for standardizing inputs to the above format
        """
        return [[[x] for x in X] for X in Xs]

    def _text_to_ids(self, *Xs, Y=None, max_length=None):
        # Maps lists of text to formatted numpy arrays of token ids and loss-masks marking the lengths of the sequences.
        max_length = max_length or self.config.max_length
        Xs = self._format_for_encoding(*Xs)
        encoder_out = self.encoder.encode_multi_input(*Xs, Y=Y, max_length=max_length)
        return self._array_format(encoder_out)

    @abstractmethod
    def _predict_op(self, logits, **kwargs):
        raise NotImplementedError

    @abstractmethod
    def _predict_proba_op(self, logits, **kwargs):
        raise NotImplementedError

    @abstractmethod
    def _target_model(self, *, featurizer_state, targets, n_outputs, train=False, reuse=None, **kwargs):
        # Overridden by subclass to attach a target model onto the shared base featurizer.
        raise NotImplementedError

    @abstractmethod
    def _target_encoder(self):
        # Overridden by subclass to produce the right target encoding for a given target model.
        raise NotImplementedError

    def _eval(self, *tensors, feed_dict):
        """
        Evaluate the value of each of the provided tensors.
        Returns a `dict` that maps from tensor to result value.  
        If any result value is None, that result is excluded from the results `dict`.
        """
        tensors = [
            tensor if tensor is not None else self.noop
            for tensor in tensors
        ]
        values = self.sess.run(tensors, feed_dict=feed_dict)
        return {
            tensor: value
            for tensor, value in zip(tensors, values)
            if value is not None
        }

    def finetune(self, *Xs, Y=None, batch_size=None):
        fit_language_model_only = (Y is None)
        arr_encoded = self._text_to_ids(*Xs)
        labels = None if fit_language_model_only else Y
        return self._training_loop(
            arr_encoded,
            Y=labels,
            batch_size=batch_size,
        )

    def _training_loop(self, arr_encoded, Y=None, batch_size=None):
        self.label_encoder = self._target_encoder()

        idxs = list(range(len(arr_encoded.token_ids)))
        train_idxs, val_idxs = train_test_split(idxs, test_size=self.config.val_size)

        if Y is None:
            # only language model will be trained, mock fake target of right length
            train_Y = np.asarray([[]] * len(train_idxs))
            val_Y = np.asarray([[]] * len(val_idxs))
            target_dim = None
        else:
            Y = np.asarray(Y)
            train_Y = self.label_encoder.fit_transform(Y[train_idxs])
            val_Y = self.label_encoder.transform(Y[val_idxs])
            target_dim = self.label_encoder.target_dim

        batch_size = batch_size or self.config.batch_size
        n_batch_train = batch_size * max(len(self.config.visible_gpus), 1)
        n_examples = len(train_idxs)
        n_updates_total = (n_examples // n_batch_train) * self.config.n_epochs

        train_dataset = (arr_encoded.token_ids[train_idxs], arr_encoded.mask[train_idxs], train_Y)
        val_dataset = (arr_encoded.token_ids[val_idxs], arr_encoded.mask[val_idxs], val_Y)

        self._build_model(n_updates_total=n_updates_total, target_dim=target_dim)
        self.is_trained = True

        avg_train_loss = None
        avg_val_loss = None
        global_step = 0
        best_val_loss = float("inf")
        val_window = [float("inf")] * self.config.val_window_size

        for i in range(self.config.n_epochs):
            iterator = iter_data(
                *train_dataset, 
                n_batch=n_batch_train, 
                tqdm_desc="Epoch {}".format(i),
                verbose=self.config.verbose
            )
            for (xmb, mmb, ymb) in iterator:
                feed_dict = {
                    self.X: xmb,
                    self.M: mmb,
                }
                if target_dim:
                    feed_dict[self.Y] = ymb

                global_step += 1
                if global_step % self.config.val_interval == 0:
                    feed_dict[self.do_dropout] = DROPOUT_OFF
                    outputs = self._eval(self.summaries, feed_dict=feed_dict)
                    if self.train_writer is not None:
                        self.train_writer.add_summary(outputs.get(self.summaries), global_step)

                    sum_val_loss = 0
                    for xval, mval, yval in iter_data(*val_dataset, n_batch=n_batch_train, verbose=self.config.verbose,
                                                      tqdm_desc="Validation"):
                        feed_dict = {
                            self.X: xval,
                            self.M: mval,
                            self.do_dropout: DROPOUT_OFF
                        }
                        if target_dim:
                            feed_dict[self.Y] = yval

                        outputs = self._eval(self.target_loss, self.summaries, feed_dict=feed_dict)

                        if self.valid_writer is not None:
                            self.valid_writer.add_summary(outputs.get(self.summaries), global_step)
                        val_cost = outputs.get(self.target_loss, 0)
                        sum_val_loss += val_cost

                        if avg_val_loss is None:
                            avg_val_loss = val_cost
                        else:
                            avg_val_loss = (
                                    avg_val_loss * self.config.rolling_avg_decay
                                    + val_cost * (1 - self.config.rolling_avg_decay)
                            )
                    val_window.append(sum_val_loss)
                    val_window.pop(0)

                    if np.mean(val_window) <= best_val_loss:
                        best_val_loss = np.mean(val_window)
                        if self.config.save_best_model:
                            self.save(self.config.autosave_path)

                    tqdm.tqdm.write("Train loss: {}\t Validation loss: {}".format(avg_train_loss, avg_val_loss))

                feed_dict[self.do_dropout] = DROPOUT_ON
                outputs = self._eval(self.target_loss, self.train_op, feed_dict=feed_dict)

                cost = outputs.get(self.target_loss, 0)
                if avg_train_loss is None:
                    avg_train_loss = cost
                else:
                    avg_train_loss = avg_train_loss * self.config.rolling_avg_decay + cost * (
                            1 - self.config.rolling_avg_decay)

        return self

    def fit(self, *args, **kwargs):
        """ An alias for finetune. """
        return self.finetune(*args, **kwargs)

    def _predict(self, *Xs, max_length=None):
        predictions = []
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore")
            max_length = max_length or self.config.max_length
            for xmb, mmb in self._infer_prep(*Xs, max_length=max_length):
                output = self._eval(self.predict_op,
                    feed_dict={
                        self.X: xmb,
                        self.M: mmb,
                        self.do_dropout: DROPOUT_OFF
                    }
                )
                prediction = output.get(self.predict_op)
                formatted_predictions = self.label_encoder.inverse_transform(prediction)
                predictions.append(formatted_predictions)
        return np.concatenate(predictions).tolist()

    def predict(self, *Xs, max_length=None):
        return self._predict(*Xs, max_length=max_length)

    def _predict_proba(self, *Xs, max_length=None):
        """
        Produce raw numeric outputs for proba predictions
        """
        predictions = []
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore")
            max_length = max_length or self.config.max_length
            for xmb, mmb in self._infer_prep(*Xs, max_length=max_length):
                output = self._eval(
                    self.predict_proba_op,
                    feed_dict={
                        self.X: xmb,
                        self.M: mmb,
                        self.do_dropout: DROPOUT_OFF
                    }
                )
                probas = output.get(self.predict_proba_op)
                predictions.extend(probas)
        return predictions

    def predict_proba(self, *args, **kwargs):
        """
        The base method for predicting from the model.
        """
        raw_probas = self._predict_proba(*args, **kwargs)
        classes = self.label_encoder.classes_

        formatted_predictions = []
        for probas in raw_probas:
            formatted_predictions.append(
                dict(zip(classes, probas))
            )
        return formatted_predictions

    def _featurize(self, *Xs, max_length=None):
        features = []
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore")
            max_length = max_length or self.config.max_length
            for xmb, mmb in self._infer_prep(*Xs, max_length=max_length):
                feature_batch = self.sess.run(self.features, {
                    self.X: xmb,
                    self.M: mmb,
                    self.do_dropout: DROPOUT_OFF
                })
                features.append(feature_batch)
        return np.concatenate(features)

    @abstractmethod
    def featurize(self, *args, **kwargs):
        """
        Base method to get raw features out of the model.
        These features are the same that are fed into the target_model.
        """
        return self._featurize(*args, **kwargs)

    def transform(self, *args, **kwargs):
        """
        An alias for `featurize`.
        """
        return self.featurize(*args, **kwargs)

    def _infer_prep(self, *Xs, max_length=None):
        max_length = max_length or self.config.max_length
        arr_encoded = self._text_to_ids(*Xs, max_length=max_length)
        n_batch_train = self.config.batch_size * max(len(self.config.visible_gpus), 1)
        self._build_model(n_updates_total=0, target_dim=self.target_dim, train=False)
        yield from iter_data(arr_encoded.token_ids, arr_encoded.mask, n_batch=n_batch_train,
                             verbose=self.config.verbose)

    def _array_format(self, encoded_output):
        """
        Returns numpy array of token idxs and corresponding mask
        Returned `x` array contains two channels:
            0: byte-pair encoding embedding
            1: positional embedding
        """
        n = len(encoded_output.token_ids)
        seq_lengths = [len(x) for x in encoded_output.token_ids]
        x = np.zeros((n, self.config.max_length, 2), dtype=np.int32)
        mask = np.zeros((n, self.config.max_length), dtype=np.float32)
        labels_arr = np.full((n, self.config.max_length), PAD_TOKEN,
                             dtype='object') if encoded_output.labels is not None else None
        for i, seq_length in enumerate(seq_lengths):
            # BPE embedding
            x[i, :seq_length, 0] = encoded_output.token_ids[i]
            # masking: value of 1 means "consider this in cross-entropy LM loss"
            mask[i, 1:seq_length] = 1
            if encoded_output.labels:
                labels_arr[i, :seq_length] = encoded_output.labels[i]
        # positional_embeddings
        x[:, :, 1] = np.arange(self.encoder.vocab_size, self.encoder.vocab_size + self.config.max_length)

        return ArrayEncodedOutput(
            token_ids=x,
            tokens=encoded_output.tokens,
            labels=labels_arr,
            char_locs=encoded_output.char_locs,
            mask=mask
        )

    def _compile_train_op(self, *, params, grads, n_updates_total, initial_params):
        grads = average_grads(grads)

        if self.config.summarize_grads:
            self.summaries += tf.contrib.training.add_gradients_summaries(grads)

        grads = [grad for grad, param in grads]
        self.train_op = AdamWeightDecay(
            params=params,
            grads=grads,
            lr=self.config.lr,
            schedule=partial(schedules[self.config.lr_schedule], warmup=self.config.lr_warmup),
            t_total=n_updates_total,
            l2=self.config.l2_reg,
            max_grad_norm=self.config.max_grad_norm,
            vector_l2=self.config.vector_l2,
            b1=self.config.b1,
            b2=self.config.b2,
            e=self.config.epsilon,
            pretrained_weights=initial_params,
            deviation_regularization=self.config.regularize_deviation,
            config=self.config
        )

    def _construct_graph(self, n_updates_total, target_dim=None, train=True, pre_trained_weights=None):
        gpu_grads = []
        self.summaries = []

        # store whether or not graph was previously compiled with dropout
        self.train = train
        self._define_placeholders(target_dim=target_dim)

        aggregator = defaultdict(list)
        train_loss_tower = 0
        gpus = self.config.visible_gpus
        n_splits = max(len(gpus), 1)

        # multi-GPU setup, using CPU as param server is most efficient unless system has direct GPU connections
        # single GPU, no need to use a different GPU as a parameter server
        params_device = 'cpu' if len(gpus) != 1 else gpus[0]

        # decide on setting for language model loss coefficient
        # if the language model loss does not contribute to overall loss,
        # remove the language model computation from the graph
        lm_loss_coef = self.config.lm_loss_coef
        if target_dim is None:
            lm_loss_coef = 1.0
        compile_lm = (train and lm_loss_coef > 0) or self.require_lm

        for i, (X, M, Y) in enumerate(soft_split(self.X, self.M, self.Y, n_splits=n_splits)):
            do_reuse = True if i > 0 else tf.AUTO_REUSE

            if gpus:
                device = tf.device(assign_to_gpu(gpus[i], params_device=params_device))
            else:
                device = tf.device('cpu')

            scope = tf.variable_scope(tf.get_variable_scope(), reuse=do_reuse)

            with device, scope:
                featurizer_state = featurizer(
                    X,
                    config=self.config,
                    encoder=self.encoder,
                    dropout_placeholder=self.do_dropout,
                    train=train,
                    reuse=do_reuse
                )

                if compile_lm:
                    language_model_state = language_model(
                        X=X,
                        M=M,
                        config=self.config,
                        embed_weights=featurizer_state['embed_weights'],
                        hidden=featurizer_state['sequence_features'],
                        reuse=do_reuse
                    )

                    train_loss = lm_loss_coef * tf.reduce_mean(language_model_state['losses'])
                    aggregator['lm_losses'].append(language_model_state['losses'])
                    lm_logits = language_model_state["logits"]
                    aggregator["lm_model"].append(sample_with_temperature(lm_logits, self.config.lm_temp))
                else:
                    train_loss = 0

                aggregator['features'].append(featurizer_state['features'])

                if target_dim is not None:
                    with tf.variable_scope('model/target'):
                        target_model_state = self._target_model(
                            featurizer_state=featurizer_state,
                            targets=Y,
                            n_outputs=target_dim,
                            train=train,
                            reuse=do_reuse,
                            max_length=self.config.max_length
                        )
                    train_loss += (1 - lm_loss_coef) * tf.reduce_mean(target_model_state['losses'])
                    train_loss_tower += train_loss

                    aggregator['logits'].append(target_model_state['logits'])
                    aggregator['target_losses'].append(target_model_state['losses'])

                params = find_trainable_variables("model")
                grads = tf.gradients(train_loss, params)
                grads = list(zip(grads, params))
                gpu_grads.append(grads)

        with tf.device(params_device):
            self.features = tf.concat(aggregator['features'], axis=0)

            if compile_lm:
                self.lm_predict_op = tf.concat(aggregator["lm_model"], 0)
                self.lm_losses = tf.concat(aggregator['lm_losses'], axis=0)
                self.lm_loss = tf.reduce_mean(self.lm_losses)
                self.summaries.append(tf.summary.scalar('LanguageModelLoss', self.lm_loss))

            if train:
                self._compile_train_op(
                    params=params,
                    grads=gpu_grads,
                    n_updates_total=n_updates_total,
                    initial_params=pre_trained_weights
                )

            if target_dim is not None:
                self.logits = tf.concat(aggregator['logits'], axis=0)
                self.target_losses = concat_or_stack(aggregator['target_losses'])

                self.predict_op = self._predict_op(
                    self.logits, **target_model_state.get("predict_params", {})
                )
                self.predict_proba_op = self._predict_proba_op(
                    self.logits, **target_model_state.get("predict_params", {})
                )
                self.target_loss = tf.reduce_mean(self.target_losses)

                self.summaries.append(tf.summary.scalar('TargetModelLoss', self.target_loss))
                self.summaries.append(tf.summary.scalar('TotalLoss', train_loss_tower / n_splits))

            self.summaries = tf.summary.merge(self.summaries) if self.summaries else self.noop

    def _build_model(self, n_updates_total, target_dim, train=True):
        """
        Construct tensorflow symbolic graph.
        """

        pre_trained_weights = self._load_base_model()
        if not self.is_trained or train != self.train or self.target_dim != target_dim:
            # reconstruct graph to include/remove dropout
            # if `train` setting has changed
            self._construct_graph(n_updates_total, target_dim, pre_trained_weights=pre_trained_weights, train=train)

        self._initialize_session()

        # Optionally load saved model
        if self._load_from_file:
            self._load_finetuned_model()
        elif not self.is_trained:
            self._init_from_pretrained(pre_trained_weights["init_params"])

        # Must be set after loading saved models -- we used the stored value of target_dim to 
        # infer what portions of the graph to attempt to load from the checkpoint
        self.target_dim = target_dim

        guarantee_initialized_variables(self.sess, keys=['model/target', 'adam'])

        if train:
            if self.config.tensorboard_folder is not None:
                if not os.path.exists(self.config.tensorboard_folder):
                    os.mkdir(self.config.tensorboard_folder)
                self.train_writer = tf.summary.FileWriter(self.config.tensorboard_folder + '/train', self.sess.graph)
                self.valid_writer = tf.summary.FileWriter(self.config.tensorboard_folder + '/valid', self.sess.graph)
        self.is_built = True

    def _initialize_session(self):
        if self.sess is None:
            gpus = self.config.visible_gpus
            os.environ['CUDA_VISIBLE_DEVICES'] = ",".join([str(gpu) for gpu in gpus])
            conf = tf.ConfigProto(allow_soft_placement=self.config.soft_device_placement,
                                  log_device_placement=self.config.log_device_placement)
            self.sess = tf.Session(config=conf)

    def _set_random_seed(self, seed=None):
        seed = seed or self.config.seed
        random.seed(seed)
        np.random.seed(seed)
        tf.set_random_seed(seed)

    def _target_placeholder(self, target_dim=None):
        return tf.placeholder(tf.float32, [None, target_dim or 1])  # classification targets

    def _define_placeholders(self, target_dim=None):
        # tf placeholders
        self.X = tf.placeholder(tf.int32, [None, self.config.max_length, 2])  # token idxs (BPE embedding + positional)
        self.M = tf.placeholder(tf.float32, [None, self.config.max_length])  # sequence mask
        # when target dim is not set, an array of [None] targets is passed as a placeholder

        self.do_dropout = tf.placeholder(tf.float32)  # 1 for do dropout and 0 to not do dropout
        self.Y = self._target_placeholder(target_dim=target_dim)

    def generate_text(self, seed_text='', max_length=None):
        """
        Performs a prediction on the Language modeling objective given some seed text. It uses a noisy greedy decoding.
        Temperature parameter for decoding is set in the config.

        :param max_length: The maximum length to decode to.
        :param seed_text: Defaults to the empty string. This will form the starting point to begin modelling
        :return: A string containing the generated text.
        """
        self.require_lm = True
        encoded = self.encoder._encode([seed_text])
        self._build_model(n_updates_total=0, target_dim=self.target_dim, train=False)
        string = [self.encoder['_start_']] + encoded.token_ids[0]
        EOS = self.encoder['_classify_']
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore")
            for i in range(len(encoded.token_ids), (max_length or self.config.max_length) - 1):
                arr_encoded = self._array_format(encoded)
                class_idx = self.sess.run(self.lm_predict_op, {self.X: arr_encoded.token_ids, self.M: arr_encoded.mask})
                string.append(class_idx[i])
                if string[-1] == EOS:
                    break
        return self.encoder.decode(string)

    def _load_base_model(self):
        """
        Load serialized base model parameters from file.
        """
        with open(SHAPES_PATH) as shapes_file:
            shapes = json.load(shapes_file)
            offsets = np.cumsum([np.prod(shape) for shape in shapes])
            init_params = [np.load(PARAM_PATH.format(n)) for n in range(10)]
            init_params = np.split(np.concatenate(init_params, 0), offsets)[:-1]
            init_params = [param.reshape(shape) for param, shape in zip(init_params, shapes)]

            if self.config.interpolate_pos_embed:
                init_params[0] = interpolate_pos_embed(init_params[0], self.config.max_length)
            elif self.config.max_length > 512:
                raise ValueError("Max Length cannot be greater than 512 if interploate_pos_embed is turned off")
            else:
                init_params[0] = init_params[0][:self.config.max_length]

            special_embed = (np.random.randn(len(self.encoder.special_tokens),
                                             self.config.n_embed) * self.config.weight_stddev).astype(np.float32)
            reg_mask = np.concatenate(
                [np.ones_like(init_params[1]), np.zeros_like(special_embed), np.ones_like(init_params[0])], 0)

            init_params[0] = np.concatenate([init_params[1], special_embed, init_params[0]], 0)
            del init_params[1]

        return {
            "init_params": init_params,
            "mask": itertools.chain([reg_mask], itertools.repeat(1.0, len(init_params) - 1))
        }

    def _init_from_pretrained(self, init_params):
        """ Load pre-trained weights into the tensors """
        pretrained_params = find_trainable_variables("model", exclude="model/target")
        self.sess.run(tf.global_variables_initializer())
        ops = []
        for p, ip in zip(pretrained_params, init_params):
            if "we" in p.name and self.config.init_embeddings_from_file is not None:
                print("Loading embeddings from file.")
                ops.append(p.assign(np.load(self.config.init_embeddings_from_file)))
            else:
                ops.append(p.assign(ip))

        self.sess.run(ops)

    def __getstate__(self):
        """
        Leave serialization of all tf objects to tf
        """
        required_fields = [
            'label_encoder', 'target_dim', '_load_from_file', 'config', 'target_type',
        ]
        serialized_state = {
            k: v for k, v in self.__dict__.items()
            if k in required_fields
        }
        return serialized_state

    def save(self, path):
        """
        Saves the state of the model to disk to the folder specific by `path`.  If `path` does not exist, it will be auto-created.

        Save is performed in two steps:
            - Serialize tf graph to disk using tf.Saver
            - Serialize python model using pickle

        Note:
            Does not serialize state of Adam optimizer.
            Should not be used to save / restore a training model.
        """
        if path is None:
            return

        # Setting self._load_from_file indicates that we should load the saved model from
        # disk at next training / inference. It is set temporarily so that the serialized
        # model includes this information.
        path = os.path.abspath(path)
        self._load_from_file = path
        saver = tf.train.Saver(tf.trainable_variables())
        path_obj = Path(path)
        if path_obj.exists():
            if not path_obj.is_dir():
                raise ValueError("Invalid save location: {}.  A file already exists at this location.".format(path))
        else:
            path_obj.mkdir(parents=True)

        saver.save(self.sess, os.path.join(path, SAVE_PREFIX))
        pickle.dump(self, open(
            os.path.join(self._load_from_file, '{}.pkl'.format(SAVE_PREFIX)), 'wb')
        )
        self._load_from_file = False

    @classmethod
    def load(cls, path):
        """
        Load a saved fine-tuned model from disk.  Path provided should be a folder which contains .pkl and tf.Saver() files

        :param path: string path name to load model from.  Same value as previously provided to :meth:`save`. Must be a folder.
        """
        pickle_path = os.path.join(path, '{}.pkl'.format(SAVE_PREFIX))
        model = pickle.load(open(pickle_path, 'rb'))
        model._initialize()
        tf.reset_default_graph()
        return model

    def _load_finetuned_model(self):
        var_list = find_trainable_variables('model', exclude='model/target')
        if self.target_dim is not None:
            var_list.extend(find_trainable_variables('model/target'))
        saver = tf.train.Saver(var_list=var_list)
        saver.restore(self.sess, os.path.join(self._load_from_file, SAVE_PREFIX))
        self._load_from_file = False
        self.is_trained = True

    @classmethod
    def finetune_grid_search(cls, config, Xs, Y, eval_fn, test_size, probs=False, return_all=False):
        trainXs, testXs, trainY, testY = train_test_split(list_transpose(Xs), Y, test_size=test_size)
        trainXs = list_transpose(trainXs)
        testXs = list_transpose(testXs)
        ranged_keys = []
        ranged_itterators = []
        for key, item in config.items():
            if hasattr(item, "get_itterator"):
                ranged_keys.append(key)
                ranged_itterators.append(item.get_itterator())
        grid_gen = itertools.product(*ranged_itterators)
        results = []
        for grid_item in grid_gen:
            tf.reset_default_graph()
            config_ = deepcopy(config)
            config_.update(dict(zip(ranged_keys, grid_item)))
            instance = cls(config=config_)
            instance.finetune(*trainXs, trainY)
            if probs:
                res = instance.predict_proba(*testXs)
            else:
                res = instance.predict(*testXs)
            results.append((config, eval_fn(res, testY)))
            del instance
        if return_all:
            return results

        return max(results, key=lambda x: x[1])[0]



