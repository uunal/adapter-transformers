import os
import unittest

import numpy as np

from transformers import (  # get_adapter_config_hash,
    ADAPTER_CONFIG_MAP,
    AdapterConfig,
    AutoModel,
    AutoTokenizer,
    BertForSequenceClassification,
    BertModelWithHeads,
    GlueDataset,
    GlueDataTrainingArguments,
    TrainingArguments,
    get_adapter_config_hash,
    glue_compute_metrics,
)
from transformers.adapters.trainer import AdapterTrainer as Trainer
from transformers.adapters.utils import find_in_index
from transformers.testing_utils import require_torch

from .test_modeling_common import ids_tensor


SAMPLE_INDEX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures/hub-index.sample.json")


@require_torch
class AdapterHubTest(unittest.TestCase):
    search_samples = [
        ("t@ukp", "pfeiffer", "path/to/pfeiffer/ukp"),
        ("s@ukp", "pfeiffer", "path/to/pfeiffer/ukp"),
        ("xyz", "pfeiffer", None),
        ("t/s", None, "path/to/default"),
        ("t/s@ukp", "pfeiffer", "path/to/pfeiffer/ukp"),
        ("t/s", "pfeiffer", "path/to/pfeiffer/default"),
        ("t/s", "houlsby", "path/to/houlsby/example-org"),
    ]

    def test_find_in_index(self):
        for sample in self.search_samples:
            with self.subTest(sample=sample):
                config = ADAPTER_CONFIG_MAP[sample[1]] if sample[1] else None
                found_entry = find_in_index(sample[0], None, config, index_file=SAMPLE_INDEX)
                self.assertEqual(sample[2], found_entry)

    def test_load_task_adapter_from_hub(self):
        """This test checks if an adapter is loaded from the Hub correctly by evaluating it on some MRPC samples
        and comparing with the expected result.
        """
        for config in ["pfeiffer", "houlsby"]:
            with self.subTest(config=config):
                tokenizer = AutoTokenizer.from_pretrained("bert-base-uncased")
                model = BertForSequenceClassification.from_pretrained("bert-base-uncased")

                loading_info = {}
                adapter_name = model.load_adapter(
                    "sts/mrpc@ukp", config=config, version="1", loading_info=loading_info
                )
                model.train_adapter(adapter_name)

                self.assertEqual(0, len(loading_info["missing_keys"]))
                self.assertEqual(0, len(loading_info["unexpected_keys"]))

                self.assertIn(adapter_name, model.config.adapters.adapters)
                self.assertNotIn(adapter_name, model.base_model.invertible_adapters)

                # check if config is valid
                expected_hash = get_adapter_config_hash(AdapterConfig.load(config))
                real_hash = get_adapter_config_hash(model.config.adapters.get(adapter_name))
                self.assertEqual(expected_hash, real_hash)

                # setup dataset
                data_args = GlueDataTrainingArguments(
                    task_name="mrpc", data_dir="./tests/fixtures/tests_samples/MRPC", overwrite_cache=True
                )
                eval_dataset = GlueDataset(data_args, tokenizer=tokenizer, mode="dev")
                training_args = TrainingArguments(output_dir="./examples", no_cuda=True)

                # evaluate
                trainer = Trainer(
                    model=model,
                    args=training_args,
                    eval_dataset=eval_dataset,
                    compute_metrics=self._compute_glue_metrics("mrpc"),
                    adapter_names=["mrpc"],
                )
                result = trainer.evaluate()
                self.assertGreater(result["eval_acc"], 0.9)

    def _compute_glue_metrics(self, task_name):
        return lambda p: glue_compute_metrics(task_name, np.argmax(p.predictions, axis=1), p.label_ids)

    def test_load_task_adapter_from_hub_with_leave_out(self):
        model = AutoModel.from_pretrained("bert-base-uncased")

        loading_info = {}
        adapter_name = model.load_adapter("sts/mrpc@ukp", config="pfeiffer", loading_info=loading_info, leave_out=[11])

        self.assertEqual(0, len(loading_info["missing_keys"]))
        # self.assertEqual(0, len(loading_info["unexpected_keys"]))
        self.assertIn(adapter_name, model.config.adapters.adapters)

        # check if leave out was applied to config
        self.assertEqual([11], model.config.adapters.get(adapter_name).leave_out)

        # layer 11 should be removed while others should still exist
        self.assertIn(adapter_name, model.base_model.encoder.layer[10].output.adapters)
        self.assertNotIn(adapter_name, model.base_model.encoder.layer[11].output.adapters)

    def test_load_lang_adapter_from_hub(self):
        for config in ["pfeiffer+inv", "houlsby+inv"]:
            with self.subTest(config=config):
                model = AutoModel.from_pretrained("bert-base-multilingual-cased")
                config = AdapterConfig.load(config, non_linearity="gelu", reduction_factor=2)

                loading_info = {}
                adapter_name = model.load_adapter(
                    "fi/wiki@ukp", config=config, set_active=True, loading_info=loading_info
                )

                self.assertEqual(0, len(loading_info["missing_keys"]))
                self.assertEqual(0, len(loading_info["unexpected_keys"]))

                # check if adapter & invertible adapter were added
                self.assertIn(adapter_name, model.config.adapters.adapters)
                self.assertIn(adapter_name, model.invertible_adapters)

                # check if config is valid
                # TODO-AH hashes are not guaranteed to be equal because of legacy keys in lang adapter config
                # expected_hash = get_adapter_config_hash(config)
                # real_hash = get_adapter_config_hash(model.config.adapters.get(adapter_name))
                # self.assertEqual(expected_hash, real_hash)

                # check size of output
                in_data = ids_tensor((1, 128), 1000)
                output = model(in_data)
                self.assertEqual([1, 128, 768], list(output[0].size()))

    def test_load_adapter_with_head_from_hub(self):
        model = BertModelWithHeads.from_pretrained("bert-base-uncased")

        loading_info = {}
        adapter_name = model.load_adapter(
            "qa/squad1@ukp", config="houlsby", version="1", set_active=True, loading_info=loading_info
        )

        self.assertEqual(0, len(loading_info["missing_keys"]))
        self.assertEqual(0, len(loading_info["unexpected_keys"]))

        self.assertIn(adapter_name, model.config.adapters.adapters)
        # check if config is valid
        expected_hash = get_adapter_config_hash(AdapterConfig.load("houlsby"))
        real_hash = get_adapter_config_hash(model.config.adapters.get(adapter_name))
        self.assertEqual(expected_hash, real_hash)

        # check size of output
        in_data = ids_tensor((1, 128), 1000)
        output = model(in_data)
        self.assertEqual([1, 128], list(output[0].size()))
