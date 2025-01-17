import copy
import random
import unittest

import torch

from tests.test_adapter_training import filter_parameters
from transformers import (
    AutoModelWithHeads,
    AutoTokenizer,
    BertConfig,
    BertForSequenceClassification,
    GlueDataset,
    GlueDataTrainingArguments,
    Trainer,
    TrainingArguments,
)
from transformers.adapters.composition import BatchSplit, Fuse, Parallel, Split, Stack, parse_composition
from transformers.testing_utils import require_torch, torch_device

from .test_modeling_common import ids_tensor


class AdapterCompositionParsingTest(unittest.TestCase):
    def test_parse_lists(self):
        self.assertEqual(Stack("a"), parse_composition("a"))
        self.assertEqual(Stack("a", "b", "c"), parse_composition(["a", "b", "c"]))
        self.assertEqual(Stack("a", Fuse("b", "c")), parse_composition(["a", ["b", "c"]]))

    def test_to_deep(self):
        self.assertRaises(ValueError, lambda: parse_composition(Stack("a", Fuse("b", Stack(Fuse("c", "d"), "e")))))

    def test_invalid_nesting_fusion(self):
        self.assertRaises(ValueError, lambda: parse_composition(Fuse(Fuse("a", "b"), "c")))
        self.assertRaises(ValueError, lambda: parse_composition(Fuse(Split("a", "b", 128), "c")))

    def test_invalid_nesting_split(self):
        self.assertRaises(ValueError, lambda: parse_composition(Split("a", Fuse("b", "c"), 128)))


@require_torch
class AdapterCompositionTest(unittest.TestCase):
    def setUp(self):
        self.model = BertForSequenceClassification(BertConfig())
        self.model.add_adapter("a")
        self.model.add_adapter("b")
        self.model.add_adapter("c")
        self.model.add_adapter("d")
        self.model.to(torch_device)
        self.model.train()

    def training_pass(self):
        inputs = {}
        inputs["input_ids"] = ids_tensor((1, 128), 1000)
        inputs["labels"] = torch.ones(1, dtype=torch.long)
        loss = self.model(**inputs).loss
        loss.backward()

    def batched_training_pass(self):
        inputs = {"input_ids": ids_tensor((4, 128), 1000), "labels": torch.ones(4, dtype=torch.long)}
        loss = self.model(**inputs).loss
        loss.backward()

    def test_simple_split(self):
        # pass over split setup
        self.model.set_active_adapters(Split("a", "b", 64))

        self.training_pass()

    def test_stacked_split(self):
        # split into two stacks
        self.model.set_active_adapters(Split(Stack("a", "b"), Stack("c", "d"), split_index=64))

        self.training_pass()

    def test_stacked_fusion(self):
        self.model.add_adapter_fusion(Fuse("b", "d"))

        # fuse two stacks
        self.model.set_active_adapters(Fuse(Stack("a", "b"), Stack("c", "d")))

        self.training_pass()

    def test_mixed_stack(self):
        self.model.add_adapter_fusion(Fuse("a", "b"))

        self.model.set_active_adapters(Stack("a", Split("c", "d", split_index=64), Fuse("a", "b")))

        self.training_pass()

    def test_nested_split(self):
        # split into two stacks
        self.model.set_active_adapters(Split(Split("a", "b", split_index=32), "c", split_index=64))

        self.training_pass()

    def test_parallel(self):
        self.model.set_active_adapters(Parallel("a", "b", "c", "d"))

        inputs = {}
        inputs["input_ids"] = ids_tensor((1, 128), 1000)
        logits = self.model(**inputs).logits
        self.assertEqual(logits.shape, (4, 2))

    def test_nested_parallel(self):
        self.model.set_active_adapters(Stack("a", Parallel(Stack("b", "c"), "d")))

        inputs = {}
        inputs["input_ids"] = ids_tensor((1, 128), 1000)
        logits = self.model(**inputs).logits
        self.assertEqual(logits.shape, (2, 2))

    def test_batch_split(self):
        self.model.set_active_adapters(BatchSplit("a", "b", "c", batch_sizes=[1, 1, 2]))
        self.batched_training_pass()

    def test_batch_split_int(self):
        self.model.set_active_adapters(BatchSplit("a", "b", batch_sizes=2))
        self.batched_training_pass()

    def test_nested_batch_split(self):
        self.model.set_active_adapters(Stack("a", BatchSplit("b", "c", batch_sizes=[2, 2])))
        self.batched_training_pass()

    def test_batch_split_invalid(self):
        self.model.set_active_adapters(BatchSplit("a", "b", batch_sizes=[3, 4]))
        with self.assertRaises(IndexError):
            self.batched_training_pass()

    def test_batch_split_equivalent(self):
        self.model.set_active_adapters("a")
        self.model.eval()
        input_ids = ids_tensor((2, 128), 1000)
        output_a = self.model(input_ids[:1])

        self.model.set_active_adapters("b")
        output_b = self.model(input_ids[1:2])

        self.model.set_active_adapters(BatchSplit("a", "b", batch_sizes=[1, 1]))
        output = self.model(input_ids)

        self.assertTrue(torch.allclose(output_a[0], output[0][0], atol=1e-6))
        self.assertTrue(torch.allclose(output_b[0], output[0][1], atol=1e-6))


@require_torch
class ParallelAdapterInferenceTestMixin:
    def test_parallel_inference_with_heads(self):
        model = AutoModelWithHeads.from_config(self.config())

        model.add_adapter("a")
        model.add_adapter("b")
        model.add_classification_head("a", num_labels=2)
        model.add_classification_head("b", num_labels=3)
        model.eval()

        inputs = self.get_input_samples((2, 128), config=model.config)
        inputs["attention_mask"] = torch.randint(0, 2, size=(2, 128))

        # for reference, pass through single adapters
        model.active_adapters = "a"
        model.active_head = "a"
        outputs_a = model(**inputs)
        model.active_adapters = "b"
        model.active_head = "b"
        outputs_b = model(**inputs)

        model.active_adapters = Parallel("a", "b")
        # active_adapters should set parallel heads too
        self.assertEqual(model.active_head, ["a", "b"])
        outputs = model(**inputs)

        self.assertEqual(len(outputs), 2)
        self.assertEqual(outputs[0][0].shape, (2, 2))
        self.assertEqual(outputs[1][0].shape, (2, 3))
        self.assertTrue(torch.allclose(outputs[0][0], outputs_a[0]))
        self.assertTrue(torch.allclose(outputs[1][0], outputs_b[0]))

    def test_parallel_inference_with_wrong_number_of_heads(self):
        model = AutoModelWithHeads.from_config(self.config())
        model.eval()

        model.add_adapter("a")
        model.add_adapter("b")
        model.add_classification_head("a", num_labels=2)

        inputs = self.get_input_samples((2, 128), config=model.config)

        model.active_adapters = Parallel("a", "b")
        model.active_head = ["a"]
        with self.assertRaises(ValueError):
            model(**inputs)

        model.active_head = "a"
        with self.assertRaises(ValueError):
            model(**inputs)

    def test_batch_split_with_heads(self):
        model = AutoModelWithHeads.from_config(self.config())
        model.add_adapter("a")
        model.add_adapter("b")
        model.add_classification_head("a", num_labels=2)
        model.add_classification_head("b", num_labels=3)
        model.eval()

        inputs = self.get_input_samples((2, 128), config=model.config)["input_ids"]

        # for reference, pass through single adapters
        model.active_adapters = "a"
        model.active_head = "a"
        outputs_a = model(inputs[:1])
        model.active_adapters = "b"
        model.active_head = "b"
        outputs_b = model(inputs[1:])

        model.set_active_adapters(BatchSplit("a", "b", batch_sizes=[1, 1]))
        output = model(inputs)

        self.assertEqual(2, len(output))
        self.assertTrue(torch.allclose(output[0]["logits"], outputs_a["logits"]))
        self.assertTrue(torch.allclose(output[1]["logits"], outputs_b["logits"]))


def create_twin_adapters(model, name):
    # create adapter
    adapter1, adapter2 = name + "_1", name + "_2"
    model.add_adapter(adapter1)
    model.add_classification_head(adapter1)
    # create a twin initialized with the same random weights
    model.add_adapter(adapter2)
    model.add_classification_head(adapter2)

    state_dict = model.state_dict()
    for k, v in state_dict.items():
        if adapter1 in k:
            state_dict[k.replace(adapter1, adapter2)] = v
    model.load_state_dict(state_dict)

    return adapter1, adapter2


class ParallelTrainingMixin:
    def train_model(self, model, dataset):
        # trains model in eval mode for 2 epochs
        random.seed(42)
        torch.manual_seed(42)
        # Depending on the used optimizer the adapters are not exactly the same
        optimizer = torch.optim.SGD(model.parameters(), lr=0.1)
        for epoch in range(2):
            for data_input in dataset:
                optimizer.zero_grad()
                output = model(**data_input)
                loss = output["loss"]
                loss.backward()
                optimizer.step()
        return model

    def test_parallel_training(self):
        tokenizer = AutoTokenizer.from_pretrained(self.tokenizer_name, use_fast=False)
        if tokenizer.pad_token is None:
            tokenizer.pad_token = tokenizer.eos_token
        model = AutoModelWithHeads.from_config(self.config())

        model.add_adapter("mrpc1")
        model.add_adapter("mrpc2")
        model.add_classification_head("mrpc1", num_labels=2)
        model.add_classification_head("mrpc2", num_labels=3)
        model.eval()
        model.active_adapters = Parallel("mrpc1", "mrpc2")
        model.train_adapter(Parallel("mrpc1", "mrpc2"))

        # all weights of the adapter should be activated
        for k, v in filter_parameters(model, "adapters.mrpc1.").items():
            self.assertTrue(v.requires_grad, k)
        # all weights of the adapter not used for training should be freezed
        for k, v in filter_parameters(model, "adapters.mrpc2.").items():
            self.assertTrue(v.requires_grad, k)
        # weights of the model should be freezed (check on some examples)
        for k, v in filter_parameters(model, "encoder.layer.0.attention").items():
            self.assertFalse(v.requires_grad, k)

        state_dict_pre = copy.deepcopy(model.state_dict())

        # setup dataset
        data_args = GlueDataTrainingArguments(
            task_name="mrpc", data_dir="./tests/fixtures/tests_samples/MRPC", overwrite_cache=True
        )
        train_dataset = GlueDataset(data_args, tokenizer=tokenizer, mode="train")
        training_args = TrainingArguments(
            output_dir="./examples", do_train=True, learning_rate=0.1, max_steps=7, no_cuda=True
        )

        # evaluate
        trainer = Trainer(
            model=model,
            args=training_args,
            train_dataset=train_dataset,
        )
        trainer.train()

        for ((k1, v1), (k2, v2)) in zip(state_dict_pre.items(), model.state_dict().items()):
            if "mrpc" in k1:
                self.assertFalse(torch.equal(v1, v2))
            else:
                self.assertTrue(torch.equal(v1, v2))

    def test_parallel_training_equivalent_to_single_adapters(self):
        model = AutoModelWithHeads.from_config(self.config())
        model.eval()

        a1, a2 = create_twin_adapters(model, "a")
        b1, b2 = create_twin_adapters(model, "b")

        dataset = []
        for i in range(3):
            input_data = self.get_input_samples((3, 128), config=model.config)
            input_data["labels"] = torch.randint(0, 2, (3, 1))
            dataset.append(input_data)

        for adapter in [a1, b1]:
            model.active_head = adapter
            model.set_active_adapters(adapter)
            model.train_adapter(adapter)
            model.eval()

            model = self.train_model(model, dataset)

        model.set_active_adapters(Parallel(a2, b2))
        model.train_adapter((Parallel(a2, b2)))
        model.eval()

        model = self.train_model(model, dataset)

        state_dict = model.state_dict()
        for k, v in state_dict.items():
            if a1 in k:
                self.assertTrue(torch.allclose(v, state_dict[k.replace(a1, a2)]))
            if b1 in k:
                self.assertTrue(torch.allclose(v, state_dict[k.replace(b1, b2)]))

    def test_parallel_training_single_forward_pass(self):
        model = AutoModelWithHeads.from_config(self.config())
        model.eval()

        a1, a2 = create_twin_adapters(model, "a")
        b1, b2 = create_twin_adapters(model, "b")

        state_dict = model.state_dict()
        for k, v in state_dict.items():
            if a1 in k:
                self.assertTrue(torch.equal(v, state_dict[k.replace(a1, a2)]))
            if b1 in k:
                self.assertTrue(torch.equal(v, state_dict[k.replace(b1, b2)]))

        input_data = self.get_input_samples((3, 128), config=model.config)
        input_data["labels"] = torch.randint(0, 2, (3, 1))

        outputs = []
        for adapter in [a1, b1]:
            model.active_head = adapter
            model.set_active_adapters(adapter)
            model.train_adapter(adapter)
            model.eval()
            outputs.append(model(**input_data))

        model.set_active_adapters(Parallel(a2, b2))
        model.train_adapter((Parallel(a2, b2)))
        model.eval()

        parallel_outputs = model(**input_data)

        for out1, out2 in zip(outputs, parallel_outputs.head_outputs):
            self.assertTrue(torch.equal(out1["loss"], out2["loss"]))
            self.assertTrue(torch.allclose(out1["logits"], out2["logits"]))
