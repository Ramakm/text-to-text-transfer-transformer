# Copyright 2020 The T5 Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Lint as: python3
"""Hugging Face Transformers T5 Model.

This model API is fully functional but should be treated as experimental and
subject to change. Due to implementation details, if you are interested in
exactly replicating the results in ``Exploring the Limits of Transfer Learning
with a Unified Text-to-Text Transformer'' you should use the MtfModel API
instead.

Usage example for fine-tuning and evaluating on CoLA:

```Python
import functools

import t5
import torch
import transformers

if torch.cuda.is_available():
    device = torch.device("cuda")
else:
    device = torch.device("cpu")

model = t5.models.HfPyTorchModel("t5-base", "/tmp/hft5/", device)

# Evaluate the pre-trained checkpoint, before further fine-tuning
model.eval(
    "glue_cola_v002",
    sequence_length={"inputs": 64, "targets": 4},
    batch_size=128,
)

# Run 1000 steps of fine-tuning
model.train(
    mixture_or_task_name="glue_cola_v002",
    steps=1000,
    save_steps=100,
    sequence_length={"inputs": 64, "targets": 4},
    split="train",
    batch_size=32,
    optimizer=functools.partial(transformers.AdamW, lr=1e-4),
)

# Evaluate after fine-tuning
model.eval(
    "glue_cola_v002",
    checkpoint_steps="all",
    sequence_length={"inputs": 64, "targets": 4},
    batch_size=128,
)

# Generate some predictions
inputs = [
    "cola sentence: This is a totally valid sentence.",
    "cola sentence: A doggy detail was walking famously.",
]
model.predict(
    inputs,
    sequence_length={"inputs": 32},
    batch_size=2,
    output_file="/tmp/hft5/example_predictions.txt",
)
```

"""

import functools
import itertools
import os
import re
import time

from absl import logging
import mesh_tensorflow.transformer.dataset as transformer_dataset
import t5.data
from t5.models.t5_model import T5Model
import tensorflow as tf
import tensorflow_datasets as tfds
import torch
import torch.utils.tensorboard
import transformers

CHECKPOINT_FILE_FORMAT = "model-{}.checkpoint"


def tokens_to_batches(dataset, sequence_length, batch_size, output_features):
  """Convert a dataset of token sequences to batches of padded/masked examples.

  Args:
    dataset: tf.data.Dataset containing examples with token sequences.
    sequence_length: dict of int, a dict mapping feature name to length.
    batch_size: int, the number of padded sequences in each batch.
    output_features: list of str, features to include in the dataset.

  Returns:
    A generator that produces batches of numpy examples.
  """
  dataset = transformer_dataset.pack_or_pad(
      dataset,
      sequence_length,
      pack=False,
      feature_keys=output_features,
      ensure_eos=True,
  )

  def _map_fn(ex):
    for key in output_features:
        tensor = ex[key]
        mask = tf.cast(tf.greater(tensor, 0), tensor.dtype)
        ex[key + "_mask"] = mask
    return ex

  dataset = dataset.map(
      _map_fn,
      num_parallel_calls=t5.data.preprocessors.num_parallel_calls()
  )

  dataset = dataset.batch(batch_size, drop_remainder=False)
  return tfds.as_numpy(dataset)


def get_dataset(mixture_or_task_name, sequence_length, split, batch_size):
  """Get a generator of numpy examples for a given Task or Mixture.

  Args:
    mixture_or_task_name: str, the name of the Mixture or Task to train on.
      Must be pre-registered in the global `t5.data.TaskRegistry` or
      `t5.data.MixtureRegistry.`
    sequence_length: dict of int, a dict mapping feature name to length.
    split: str or `tensorflow_datasets.Split`, the data split to load.
    batch_size: int, the number of padded sequences in each batch.

  Returns:
    A generator that produces batches of numpy examples.
  """
  task = t5.data.get_mixture_or_task(mixture_or_task_name)
  ds = task.get_dataset(sequence_length, split)
  return tokens_to_batches(
      ds, sequence_length, batch_size, tuple(task.output_features)
  )


def write_lines_to_file(lines, filename):
  """Write each line to filename, replacing the file if it exists."""
  if tf.io.gfile.exists(filename):
    tf.io.gfile.remove(filename)
  with tf.io.gfile.GFile(filename, "w") as output_file:
    output_file.write("\n".join([str(l) for l in lines]))


class HfPyTorchModel(T5Model):
  """Wrapper class for Hugging Face Transformers PyTorch T5 model."""

  def __init__(self, model_spec, model_dir, device):
    """Constructor for HfModel class.

    Args:
      model_spec: A str to pass into the `pretrained_model_name_or_path`
        argument of `transformers.T5ForConditionalGeneration.from_pretrained`
        (e.g. `"t5-base"` or a path to a previously trained model) or an
        instance of the `transformers.configuration_t5.T5Config` class to use
        to directly construct the `transformers.T5ForConditionalGeneration`
        object.
      model_dir: str, directory to save and load model checkpoints.
      device: `torch.device` on which the model should be run.
    """
    if isinstance(model_spec, str):
      self._model = transformers.T5ForConditionalGeneration.from_pretrained(
          model_spec
      )
    elif isinstance(model_spec, transformers.T5Config):
      self._model = transformers.T5ForConditionalGeneration(model_spec)
    else:
      raise ValueError("model_spec should be a string or T5Config.")

    tf.io.gfile.makedirs(model_dir)
    self._writer = torch.utils.tensorboard.writer.SummaryWriter(model_dir)
    self._model_dir = model_dir
    self._device = device
    if self._device.type == "cuda":
      self._model.cuda()
    self._step = 0
    self.load_latest_checkpoint()
    self.to_tensor = functools.partial(torch.as_tensor, device=self._device)

  @property
  def model(self):
    return self._model

  @property
  def step(self):
    return self._step

  def save_checkpoint(self, step):
    """Save the current model parameters to the `model_dir`.

    Args:
      step: int, the current training step.
    """
    path = os.path.join(self._model_dir, CHECKPOINT_FILE_FORMAT.format(step))
    torch.save(self._model.state_dict(), path)

  def load_checkpoint(self, step, model_dir=None):
    """Load the model parameters from a checkpoint at a given step.

    Args:
      step: int, load the checkpoint from this training step.
      model_dir: str, the directory of the checkpoint to load or None to use
        this model's directory.
    """
    model_dir = model_dir or self._model_dir
    path = os.path.join(model_dir, CHECKPOINT_FILE_FORMAT.format(step))
    logging.info("Loading from %s", path)
    self._model.load_state_dict(torch.load(path))
    self._step = step

  def get_all_checkpoint_steps(self, model_dir=None):
    """Retrieve the steps corresponding to all checkpoints in `model_dir`.

    Args:
      model_dir: str, the directory of the checkpoints or None to use this
        model's directory.

    Returns:
      A list of ints corresponding to all checkpoint steps, or None if there
        are no checkpoints in the model directory.
    """
    model_dir = model_dir or self._model_dir
    checkpoint_files = tf.io.gfile.glob(
        os.path.join(model_dir, CHECKPOINT_FILE_FORMAT.format("*"))
    )
    if not checkpoint_files:
      return
    step_regex = re.compile(".*" + CHECKPOINT_FILE_FORMAT.format(r"(\d+)"))
    steps = [int(step_regex.match(path).group(1)) for path in checkpoint_files]
    return sorted(steps)

  def get_latest_checkpoint_step(self, model_dir=None):
    """Retrieve the step corresponding to the most recent checkpoint.

    Args:
      model_dir: str, the directory of the checkpoints or None to use this
        model's directory.

    Returns:
      An integer corresponding to the most recent step, or None if there are no
      checkpoints in the model directory.
    """
    steps = self.get_all_checkpoint_steps(model_dir)
    if steps is not None:
      return max(steps)

  def load_latest_checkpoint(self):
    """Load the most recent checkpoint and update the model's current step."""
    latest_step = self.get_latest_checkpoint_step()
    if latest_step is not None:
      self.load_checkpoint(latest_step)

  def train(
      self,
      mixture_or_task_name,
      steps,
      save_steps,
      sequence_length,
      split,
      batch_size,
      optimizer,
      learning_rate_scheduler=None,
  ):
    """Train the model on the given Mixture or Task.

    Args:
      mixture_or_task_name: str, the name of the Mixture or Task to train on.
        Must be pre-registered in the global `t5.data.TaskRegistry` or
        `t5.data.MixtureRegistry.`
      steps: int, the total number of steps to train for.
      save_steps: int, the number of steps between checkpoint saves.
      sequence_length: dict of int, a dict mapping feature name to length.
      split: str or `tensorflow_datasets.Split`, the data split to load.
      batch_size: int, the number of padded sequences in each batch.
      optimizer: function that takes the model parameters as its sole argument.
        For example, to use an AdamW optimizer with a learning rate of 1e-4,
        you could pass in `functools.partial(transformers.AdamW, lr=1e-4)`.
      learning_rate_scheduler: optional function that takes in an optimizer as
        its sole argument. For example, to use a schedule that warms up the
        optimizer's learning rate after 100 steps, you could pass in
        `functools.partial(transformers.get_constant_schedule_with_warmup,
       num_warmup_steps=100)`.
    """
    self._model.train()
    ds = get_dataset(mixture_or_task_name, sequence_length, split, batch_size)
    # Repeat dataset forever
    ds = itertools.cycle(ds)
    optimizer = optimizer(self._model.parameters())
    if learning_rate_scheduler:
      learning_rate_scheduler = learning_rate_scheduler(optimizer)

    now = time.time()
    for train_step, batch in enumerate(itertools.islice(ds, steps)):

        if not train_step % save_steps:
          # TODO(craffel): Consider saving optimizer and scheduler state.
          logging.info(f"Saving checkpoint for step {self._step}")
          self.save_checkpoint(self._step)

        self._model.zero_grad()
        loss, _, _ = self._model(
            input_ids=self.to_tensor(batch["inputs"]),
            attention_mask=self.to_tensor(batch["inputs_mask"]),
            decoder_attention_mask=self.to_tensor(batch["targets_mask"]),
            lm_labels=self.to_tensor(batch["targets"]),
        )
        loss.backward()
        optimizer.step()
        if learning_rate_scheduler:
          learning_rate_scheduler.step()

        self._writer.add_scalar(
            "loss", loss.detach().cpu().numpy(), self._step
        )
        self._writer.add_scalar("step/s", 1 / (time.time() - now), self._step)
        now = time.time()
        self._step += 1

    # Save a final checkpoint step if we didn't just save one
    if train_step % save_steps:
      logging.info(f"Saving final checkpoint for step {self._step}")
      self.save_checkpoint(self._step)

  def eval(
      self,
      mixture_or_task_name,
      sequence_length,
      batch_size,
      checkpoint_steps=None,
      summary_dir=None,
      split="validation",
      **generate_kwargs,
  ):
    """Evaluate the model on the given Mixture or Task.

    *Note*: If a checkpoint step is provided (i.e. `checkpoint_steps is not
    None`), the model's state will be replaced by the state in those
    checkpoints. If you have not saved your model before calling `eval`, you
    should call `save_checkpoint` before `eval` to avoid losing its parameter
    values and state.

    Args:
      mixture_or_task_name: str, the name of the Mixture or Task to evaluate
        on.  Must be pre-registered in the global `t5.data.TaskRegistry` or
        `t5.data.MixtureRegistry.`
      sequence_length: dict of int, a dict mapping feature name to length.
      batch_size: int, the number of padded sequences in each batch.
      checkpoint_steps: int, list of ints, "all", or None. If None, eval in the
        model in its current state without loading any checkpoints. If an int
        or list of ints, evaluation will be run on the checkpoint files in
        `model_dir` whose global steps are those provided. If -1, eval on the
        latest checkpoint from the model directory. If "all", evaluate all
        checkpoints in the model directory.
      summary_dir: str, path to write TensorBoard events file summaries for
        eval. If None, use model_dir/{split}_eval.
      split: str, the mixture/task split to evaluate on.
      **generate_kwargs: Additional keyword arguments to pass to
        `transformers.PretrainedModel.generate()`, for example to change the
        decoding strategy. See the documentation for
        `transformers.PretrainedModel.generate()` for options.
    """
    mixture_or_task = t5.data.get_mixture_or_task(mixture_or_task_name)
    vocab = t5.data.sentencepiece_vocabulary.SentencePieceVocabulary(
        mixture_or_task.sentencepiece_model_path
    )

    if isinstance(mixture_or_task, t5.data.Mixture):
      tasks = mixture_or_task.tasks
    elif isinstance(mixture_or_task, t5.data.Task):
      tasks = [mixture_or_task]

    for task in tasks:
      if split not in task.splits:
        logging.info(
            "Task {task.name} has no '{split}' split; skipping eval."
        )
    tasks = [task for task in tasks if split in task.splits]

    summary_dir = summary_dir or os.path.join(self._model_dir, f"{split}_eval")
    tf.io.gfile.makedirs(summary_dir)

    def _unbatch(batch):
      """Converts a dict of lists to a list of dicts of singletons."""
      return [dict(zip(batch, t)) for t in zip(*batch.values())]

    # Pre-load in all of the targets once before doing eval
    cached_targets = {}
    cached_examples = {}
    for task in tasks:
      if task.metric_fns:
        ds = get_dataset(task.name, sequence_length, split, batch_size)
        # Create list of postprocessed text targets
        batches = list(ds)
        if not batches:
          raise ValueError(f"The '{split}' split of {task.name} is empty.")
        # "Unbatch" the dataset
        examples = [ex for b in batches for ex in _unbatch(b)]
        targets = [
            task.postprocess_fn(
                tf.compat.as_text(ex["targets_plaintext"]),
                example=ex,
                is_target=True
            ) for ex in examples
        ]
        targets_filename = os.path.join(summary_dir, f"{task.name}_targets")
        write_lines_to_file(targets, targets_filename)

        inputs_filename = os.path.join(summary_dir, f"{task.name}_inputs")
        inputs = [ex["inputs_plaintext"] for ex in examples]
        write_lines_to_file(inputs, inputs_filename)

        cached_targets[task.name] = targets
        cached_examples[task.name] = batches

    def _eval_current_model():
      self._model.eval()
      for task in tasks:
        ds = cached_examples[task.name]
        targets = cached_targets[task.name]
        predictions = []
        for batch in ds:
          predicted_tokens = self._model.generate(
              input_ids=self.to_tensor(batch["inputs"]), **generate_kwargs
          )
          predicted_tokens = predicted_tokens.cpu().numpy().tolist()
          predictions.extend(
              [
                  task.postprocess_fn(vocab.decode(p), example=ex)
                  for p, ex in zip(predicted_tokens, _unbatch(batch))
              ]
          )

        if len(targets) != len(predictions):
          raise ValueError(
              f"#targets ({len(targets)}) != #predictions ({len(predictions)})"
          )

        predictions_file = os.path.join(
            summary_dir, f"{task.name}_{self._step}_predictions"
        )
        write_lines_to_file(predictions, predictions_file)

        for metric_fn in task.metric_fns:
          scores = metric_fn(targets, predictions)
          for metric_name, metric_value in scores.items():
            tag = f"eval/{task.name}/{metric_name}"
            self._writer.add_scalar(tag, metric_value, self._step)
            logging.info(
                "%s at step %d: %.3f", tag, self._step, metric_value
            )

        self._writer.flush()

    if checkpoint_steps is None:
      _eval_current_model()
      return
    elif isinstance(checkpoint_steps, int):
      checkpoint_steps = [checkpoint_steps]
    elif checkpoint_steps == "all":
      checkpoint_steps = self.get_all_checkpoint_steps()
    elif not isinstance(checkpoint_steps, (list, tuple)):
      raise ValueError(
          f"checkpoint_steps must be None, int or list; got {checkpoint_steps}"
      )
    for checkpoint_step in checkpoint_steps:
      self.load_checkpoint(checkpoint_step)
      _eval_current_model()

  def predict(
      self,
      inputs,
      sequence_length,
      batch_size,
      output_file=None,
      sentencepiece_model_path=None,
      **generate_kwargs,
  ):
    """Evaluate the model on the given Mixture or Task.

    *Note*: If a checkpoint step is provided (i.e. `checkpoint_steps is not
    None`), the model's state will be replaced by the state in those
    checkpoints. If you have not saved your model before calling `eval`, you
    should call `save_checkpoint` before `eval` to avoid losing its parameter
    values and state.

    Args:
      inputs: list of str or str, either a list of inputs to feed into the
        model or the path to a text file that contains a single input on each
        line.
      sequence_length: dict of int, a dict mapping feature name to length.
      batch_size: int, the number of padded sequences in each batch.
      output_file: str or None, path to write out predictions or None to skip
        writing.
      sentencepiece_model_path: str or None, path to a SentencePiece model file
        or None to use `t5.data.DEFAULT_SPM_PATH` (the model used for all
        pre-trained T5 models.)
      **generate_kwargs: Additional keyword arguments to pass to
        `transformers.PretrainedModel.generate()`, for example to change the
        decoding strategy. See the documentation for
        `transformers.PretrainedModel.generate()` for options.
    """
    if isinstance(inputs, str):
      if not tf.io.gfile.exists(inputs):
        raise ValueError(
            f"A str was provided for `inputs`, but the path {inputs} does not "
            "exist. If you want the model's output for {inputs}, you should "
            "feed in inputs=['{inputs}']"
        )
      with tf.io.gfile.GFile(inputs) as f:
        inputs = [l.strip() for l in f]

    if sentencepiece_model_path is None:
      sentencepiece_model_path = t5.data.DEFAULT_SPM_PATH
    vocab = t5.data.sentencepiece_vocabulary.SentencePieceVocabulary(
        sentencepiece_model_path
    )

    dataset = tf.data.Dataset.from_tensor_slices(inputs)
    dataset = dataset.map(
        lambda ex: {"inputs": tf.cast(vocab.encode_tf(ex), tf.int64)},
        num_parallel_calls=t5.data.preprocessors.num_parallel_calls()
    )
    dataset = tokens_to_batches(
        dataset, sequence_length, batch_size, ["inputs"]
    )

    predictions = []
    for batch in dataset:
      predicted_tokens = self._model.generate(
          input_ids=self.to_tensor(batch["inputs"]), **generate_kwargs
      )
      predicted_tokens = predicted_tokens.cpu().numpy().tolist()
      predictions.extend([vocab.decode(p) for p in predicted_tokens])

    for inp, pred in zip(inputs, predictions):
      logging.info("%s\n  -> %s", inp, pred)

    if output_file is not None:
      write_lines_to_file(predictions, output_file)

  def finetune(
      self,
      mixture_or_task_name,
      finetune_steps,
      pretrained_model_dir,
      pretrained_checkpoint_step=-1,
      **train_kwargs,
  ):
    """Trains model after loading from any existing checkpoint.

    Note that if you have initialized the model using a pre-trained model
    specification (e.g. by passing "t5-base" for `model_spec`) then you can
    just call `train` directly. This function is only provided for convenience
    for loading a pre-trained model checkpoint from an arbitrary model
    directory before calling `train`.

    Args:
      mixture_or_task_name: str, the name of the Mixture or Task to evaluate
        on.  Must be pre-registered in the global `t5.data.TaskRegistry` or
        `t5.data.MixtureRegistry.`
      finetune_steps: int, the number of additional steps to train for.
      pretrained_model_dir: str, directory with pretrained model checkpoints.
      pretrained_checkpoint_step: int, checkpoint to initialize weights from.
        If -1 (default), use the latest checkpoint from the pretrained model
        directory.
      **train_kwargs: Additional keyword arguments to pass to `train`. See the
        docstring for `train` for more details.
    """
    if pretrained_checkpoint_step == -1:
      pretrained_checkpoint_step = self.get_latest_checkpoint_step(
          pretrained_model_dir
      )
    self.load_checkpoint(pretrained_checkpoint_step, pretrained_model_dir)
    self.train(mixture_or_task_name, finetune_steps, **train_kwargs)