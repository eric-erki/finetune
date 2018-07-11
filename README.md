Finetune
========

Finetune is a python library designed to make finetuning pre-trained language models
for custom natural language processing tasks easier.

It ships with pre-trained model weights
from ["Improving Language Understanding by Generative Pre-Training"](https://s3-us-west-2.amazonaws.com/openai-assets/research-covers/language-unsupervised/language_understanding_paper.pdf)
and builds off the [OpenAI/finetune-language-model repository](https://github.com/openai/finetune-transformer-lm).


Installation
============
Finetune can be installed directly from PyPI by using `pip`

```
pip3 install finetune
```

or installed directly from source:

```bash
git clone https://github.com/IndicoDataSolutions/finetune
cd finetune
python3 setup.py develop
python3 -m spacy download en
```

You can optionally run the provided test suite to ensure installation completed successfully.

```bash
pip3 install nose
nosetests
```

Finetune Quickstart Guide
=========================

Finetuning the base language model is as easy as calling `LanguageModelClassifier.fit`:

```python3
model = LanguageModelClassifier()   # load base model
model.fit(trainX, trainY)           # finetune base model on custom data
predictions = model.predict(testX)  # predict on unseen examples
# [{'class_1': 0.23, 'class_2': 0.54, 'class_3': 0.13}, ...]
model.save(path)                    # serialize the model to disk
```

Easily reload saved models from disk by using `LanguageModelClassifier.load`:

```
model = LanguageModelClassifier.load(path)
predictions = model.predict(testX)
```