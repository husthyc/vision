import contextlib
import functools
import io
import operator
import os
import pkgutil
import sys
import traceback
import warnings
from collections import OrderedDict

import pytest
import torch
import torch.fx
import torch.nn as nn
from _utils_internal import get_relative_path
from common_utils import map_nested_tensor_object, freeze_rng_state, set_rng_seed, cpu_and_gpu, needs_cuda
from torchvision import models

ACCEPT = os.getenv("EXPECTTEST_ACCEPT", "0") == "1"


def get_models_from_module(module):
    # TODO add a registration mechanism to torchvision.models
    return [v for k, v in module.__dict__.items() if callable(v) and k[0].lower() == k[0] and k[0] != "_"]


@pytest.fixture
def disable_weight_loading(mocker):
    """When testing models, the two slowest operations are the downloading of the weights to a file and loading them
    into the model. Unless, you want to test against specific weights, these steps can be disabled without any
    drawbacks.

    Including this fixture into the signature of your test, i.e. `test_foo(disable_weight_loading)`, will recurse
    through all models in `torchvision.models` and will patch all occurrences of the function
    `download_state_dict_from_url` as well as the method `load_state_dict` on all subclasses of `nn.Module` to be
    no-ops.

    .. warning:

        Loaded models are still executable as normal, but will always have random weights. Make sure to not use this
        fixture if you want to compare the model output against reference values.

    """
    starting_point = models
    function_name = "load_state_dict_from_url"
    method_name = "load_state_dict"

    module_names = {info.name for info in pkgutil.walk_packages(starting_point.__path__, f"{starting_point.__name__}.")}
    targets = {f"torchvision._internally_replaced_utils.{function_name}", f"torch.nn.Module.{method_name}"}
    for name in module_names:
        module = sys.modules.get(name)
        if not module:
            continue

        if function_name in module.__dict__:
            targets.add(f"{module.__name__}.{function_name}")

        targets.update(
            {
                f"{module.__name__}.{obj.__name__}.{method_name}"
                for obj in module.__dict__.values()
                if isinstance(obj, type) and issubclass(obj, nn.Module) and method_name in obj.__dict__
            }
        )

    for target in targets:
        # See https://github.com/pytorch/vision/pull/4867#discussion_r743677802 for details
        with contextlib.suppress(AttributeError):
            mocker.patch(target)


def _get_expected_file(name=None):
    # Determine expected file based on environment
    expected_file_base = get_relative_path(os.path.realpath(__file__), "expect")

    # Note: for legacy reasons, the reference file names all had "ModelTest.test_" in their names
    # We hardcode it here to avoid having to re-generate the reference files
    expected_file = expected_file = os.path.join(expected_file_base, "ModelTester.test_" + name)
    expected_file += "_expect.pkl"

    if not ACCEPT and not os.path.exists(expected_file):
        raise RuntimeError(
            f"No expect file exists for {os.path.basename(expected_file)} in {expected_file}; "
            "to accept the current output, re-run the failing test after setting the EXPECTTEST_ACCEPT "
            "env variable. For example: EXPECTTEST_ACCEPT=1 pytest test/test_models.py -k alexnet"
        )

    return expected_file


def _assert_expected(output, name, prec):
    """Test that a python value matches the recorded contents of a file
    based on a "check" name. The value must be
    pickable with `torch.save`. This file
    is placed in the 'expect' directory in the same directory
    as the test script. You can automatically update the recorded test
    output using an EXPECTTEST_ACCEPT=1 env variable.
    """
    expected_file = _get_expected_file(name)

    if ACCEPT:
        filename = {os.path.basename(expected_file)}
        print(f"Accepting updated output for {filename}:\n\n{output}")
        torch.save(output, expected_file)
        MAX_PICKLE_SIZE = 50 * 1000  # 50 KB
        binary_size = os.path.getsize(expected_file)
        if binary_size > MAX_PICKLE_SIZE:
            raise RuntimeError(f"The output for {filename}, is larger than 50kb")
    else:
        expected = torch.load(expected_file)
        rtol = atol = prec
        torch.testing.assert_close(output, expected, rtol=rtol, atol=atol, check_dtype=False)


def _check_jit_scriptable(nn_module, args, unwrapper=None, skip=False):
    """Check that a nn.Module's results in TorchScript match eager and that it can be exported"""

    def assert_export_import_module(m, args):
        """Check that the results of a model are the same after saving and loading"""

        def get_export_import_copy(m):
            """Save and load a TorchScript model"""
            buffer = io.BytesIO()
            torch.jit.save(m, buffer)
            buffer.seek(0)
            imported = torch.jit.load(buffer)
            return imported

        m_import = get_export_import_copy(m)
        with freeze_rng_state():
            results = m(*args)
        with freeze_rng_state():
            results_from_imported = m_import(*args)
        tol = 3e-4
        torch.testing.assert_close(results, results_from_imported, atol=tol, rtol=tol)

    TEST_WITH_SLOW = os.getenv("PYTORCH_TEST_WITH_SLOW", "0") == "1"
    if not TEST_WITH_SLOW or skip:
        # TorchScript is not enabled, skip these tests
        msg = (
            f"The check_jit_scriptable test for {nn_module.__class__.__name__} was skipped. "
            "This test checks if the module's results in TorchScript "
            "match eager and that it can be exported. To run these "
            "tests make sure you set the environment variable "
            "PYTORCH_TEST_WITH_SLOW=1 and that the test is not "
            "manually skipped."
        )
        warnings.warn(msg, RuntimeWarning)
        return None

    sm = torch.jit.script(nn_module)

    with freeze_rng_state():
        eager_out = nn_module(*args)

    with freeze_rng_state():
        script_out = sm(*args)
        if unwrapper:
            script_out = unwrapper(script_out)

    torch.testing.assert_close(eager_out, script_out, atol=1e-4, rtol=1e-4)
    assert_export_import_module(sm, args)


def _check_fx_compatible(model, inputs):
    model_fx = torch.fx.symbolic_trace(model)
    out = model(inputs)
    out_fx = model_fx(inputs)
    torch.testing.assert_close(out, out_fx)


def _check_input_backprop(model, inputs):
    if isinstance(inputs, list):
        requires_grad = list()
        for inp in inputs:
            requires_grad.append(inp.requires_grad)
            inp.requires_grad_(True)
    else:
        requires_grad = inputs.requires_grad
        inputs.requires_grad_(True)

    out = model(inputs)

    if isinstance(out, dict):
        out["out"].sum().backward()
    else:
        if isinstance(out[0], dict):
            out[0]["scores"].sum().backward()
        else:
            out[0].sum().backward()

    if isinstance(inputs, list):
        for i, inp in enumerate(inputs):
            assert inputs[i].grad is not None
            inp.requires_grad_(requires_grad[i])
    else:
        assert inputs.grad is not None
        inputs.requires_grad_(requires_grad)


# If 'unwrapper' is provided it will be called with the script model outputs
# before they are compared to the eager model outputs. This is useful if the
# model outputs are different between TorchScript / Eager mode
script_model_unwrapper = {
    "googlenet": lambda x: x.logits,
    "inception_v3": lambda x: x.logits,
    "fasterrcnn_resnet50_fpn": lambda x: x[1],
    "fasterrcnn_mobilenet_v3_large_fpn": lambda x: x[1],
    "fasterrcnn_mobilenet_v3_large_320_fpn": lambda x: x[1],
    "maskrcnn_resnet50_fpn": lambda x: x[1],
    "keypointrcnn_resnet50_fpn": lambda x: x[1],
    "retinanet_resnet50_fpn": lambda x: x[1],
    "ssd300_vgg16": lambda x: x[1],
    "ssdlite320_mobilenet_v3_large": lambda x: x[1],
}


# The following models exhibit flaky numerics under autocast in _test_*_model harnesses.
# This may be caused by the harness environment (e.g. num classes, input initialization
# via torch.rand), and does not prove autocast is unsuitable when training with real data
# (autocast has been used successfully with real data for some of these models).
# TODO:  investigate why autocast numerics are flaky in the harnesses.
#
# For the following models, _test_*_model harnesses skip numerical checks on outputs when
# trying autocast. However, they still try an autocasted forward pass, so they still ensure
# autocast coverage suffices to prevent dtype errors in each model.
autocast_flaky_numerics = (
    "inception_v3",
    "resnet101",
    "resnet152",
    "wide_resnet101_2",
    "deeplabv3_resnet50",
    "deeplabv3_resnet101",
    "deeplabv3_mobilenet_v3_large",
    "fcn_resnet50",
    "fcn_resnet101",
    "lraspp_mobilenet_v3_large",
    "maskrcnn_resnet50_fpn",
)

# The tests for the following quantized models are flaky possibly due to inconsistent
# rounding errors in different platforms. For this reason the input/output consistency
# tests under test_quantized_classification_model will be skipped for the following models.
quantized_flaky_models = ("inception_v3", "resnet50")


# The following contains configuration parameters for all models which are used by
# the _test_*_model methods.
_model_params = {
    "inception_v3": {"input_shape": (1, 3, 299, 299)},
    "retinanet_resnet50_fpn": {
        "num_classes": 20,
        "score_thresh": 0.01,
        "min_size": 224,
        "max_size": 224,
        "input_shape": (3, 224, 224),
    },
    "keypointrcnn_resnet50_fpn": {
        "num_classes": 2,
        "min_size": 224,
        "max_size": 224,
        "box_score_thresh": 0.15,
        "input_shape": (3, 224, 224),
    },
    "fasterrcnn_resnet50_fpn": {
        "num_classes": 20,
        "min_size": 224,
        "max_size": 224,
        "input_shape": (3, 224, 224),
    },
    "maskrcnn_resnet50_fpn": {
        "num_classes": 10,
        "min_size": 224,
        "max_size": 224,
        "input_shape": (3, 224, 224),
    },
    "fasterrcnn_mobilenet_v3_large_fpn": {
        "box_score_thresh": 0.02076,
    },
    "fasterrcnn_mobilenet_v3_large_320_fpn": {
        "box_score_thresh": 0.02076,
        "rpn_pre_nms_top_n_test": 1000,
        "rpn_post_nms_top_n_test": 1000,
    },
}


# The following contains configuration and expected values to be used tests that are model specific
_model_tests_values = {
    "retinanet_resnet50_fpn": {
        "max_trainable": 5,
        "n_trn_params_per_layer": [36, 46, 65, 78, 88, 89],
    },
    "keypointrcnn_resnet50_fpn": {
        "max_trainable": 5,
        "n_trn_params_per_layer": [48, 58, 77, 90, 100, 101],
    },
    "fasterrcnn_resnet50_fpn": {
        "max_trainable": 5,
        "n_trn_params_per_layer": [30, 40, 59, 72, 82, 83],
    },
    "maskrcnn_resnet50_fpn": {
        "max_trainable": 5,
        "n_trn_params_per_layer": [42, 52, 71, 84, 94, 95],
    },
    "fasterrcnn_mobilenet_v3_large_fpn": {
        "max_trainable": 6,
        "n_trn_params_per_layer": [22, 23, 44, 70, 91, 97, 100],
    },
    "fasterrcnn_mobilenet_v3_large_320_fpn": {
        "max_trainable": 6,
        "n_trn_params_per_layer": [22, 23, 44, 70, 91, 97, 100],
    },
    "ssd300_vgg16": {
        "max_trainable": 5,
        "n_trn_params_per_layer": [45, 51, 57, 63, 67, 71],
    },
    "ssdlite320_mobilenet_v3_large": {
        "max_trainable": 6,
        "n_trn_params_per_layer": [96, 99, 138, 200, 239, 257, 266],
    },
}


def _make_sliced_model(model, stop_layer):
    layers = OrderedDict()
    for name, layer in model.named_children():
        layers[name] = layer
        if name == stop_layer:
            break
    new_model = torch.nn.Sequential(layers)
    return new_model


@pytest.mark.parametrize("model_fn", [models.densenet121, models.densenet169, models.densenet201, models.densenet161])
def test_memory_efficient_densenet(model_fn):
    input_shape = (1, 3, 300, 300)
    x = torch.rand(input_shape)

    model1 = model_fn(num_classes=50, memory_efficient=True)
    params = model1.state_dict()
    num_params = sum(x.numel() for x in model1.parameters())
    model1.eval()
    out1 = model1(x)
    out1.sum().backward()
    num_grad = sum(x.grad.numel() for x in model1.parameters() if x.grad is not None)

    model2 = model_fn(num_classes=50, memory_efficient=False)
    model2.load_state_dict(params)
    model2.eval()
    out2 = model2(x)

    assert num_params == num_grad
    torch.testing.assert_close(out1, out2, rtol=0.0, atol=1e-5)

    _check_input_backprop(model1, x)
    _check_input_backprop(model2, x)


@pytest.mark.parametrize("dilate_layer_2", (True, False))
@pytest.mark.parametrize("dilate_layer_3", (True, False))
@pytest.mark.parametrize("dilate_layer_4", (True, False))
def test_resnet_dilation(dilate_layer_2, dilate_layer_3, dilate_layer_4):
    # TODO improve tests to also check that each layer has the right dimensionality
    model = models.resnet50(replace_stride_with_dilation=(dilate_layer_2, dilate_layer_3, dilate_layer_4))
    model = _make_sliced_model(model, stop_layer="layer4")
    model.eval()
    x = torch.rand(1, 3, 224, 224)
    out = model(x)
    f = 2 ** sum((dilate_layer_2, dilate_layer_3, dilate_layer_4))
    assert out.shape == (1, 2048, 7 * f, 7 * f)


def test_mobilenet_v2_residual_setting():
    model = models.mobilenet_v2(inverted_residual_setting=[[1, 16, 1, 1], [6, 24, 2, 2]])
    model.eval()
    x = torch.rand(1, 3, 224, 224)
    out = model(x)
    assert out.shape[-1] == 1000


@pytest.mark.parametrize("model_fn", [models.mobilenet_v2, models.mobilenet_v3_large, models.mobilenet_v3_small])
def test_mobilenet_norm_layer(model_fn):
    model = model_fn()
    assert any(isinstance(x, nn.BatchNorm2d) for x in model.modules())

    def get_gn(num_channels):
        return nn.GroupNorm(32, num_channels)

    model = model_fn(norm_layer=get_gn)
    assert not (any(isinstance(x, nn.BatchNorm2d) for x in model.modules()))
    assert any(isinstance(x, nn.GroupNorm) for x in model.modules())


def test_inception_v3_eval():
    # replacement for models.inception_v3(pretrained=True) that does not download weights
    kwargs = {}
    kwargs["transform_input"] = True
    kwargs["aux_logits"] = True
    kwargs["init_weights"] = False
    name = "inception_v3"
    model = models.Inception3(**kwargs)
    model.aux_logits = False
    model.AuxLogits = None
    model = model.eval()
    x = torch.rand(1, 3, 299, 299)
    _check_jit_scriptable(model, (x,), unwrapper=script_model_unwrapper.get(name, None))
    _check_input_backprop(model, x)


def test_fasterrcnn_double():
    model = models.detection.fasterrcnn_resnet50_fpn(num_classes=50, pretrained_backbone=False)
    model.double()
    model.eval()
    input_shape = (3, 300, 300)
    x = torch.rand(input_shape, dtype=torch.float64)
    model_input = [x]
    out = model(model_input)
    assert model_input[0] is x
    assert len(out) == 1
    assert "boxes" in out[0]
    assert "scores" in out[0]
    assert "labels" in out[0]
    _check_input_backprop(model, model_input)


def test_googlenet_eval():
    # replacement for models.googlenet(pretrained=True) that does not download weights
    kwargs = {}
    kwargs["transform_input"] = True
    kwargs["aux_logits"] = True
    kwargs["init_weights"] = False
    name = "googlenet"
    model = models.GoogLeNet(**kwargs)
    model.aux_logits = False
    model.aux1 = None
    model.aux2 = None
    model = model.eval()
    x = torch.rand(1, 3, 224, 224)
    _check_jit_scriptable(model, (x,), unwrapper=script_model_unwrapper.get(name, None))
    _check_input_backprop(model, x)


@needs_cuda
def test_fasterrcnn_switch_devices():
    def checkOut(out):
        assert len(out) == 1
        assert "boxes" in out[0]
        assert "scores" in out[0]
        assert "labels" in out[0]

    model = models.detection.fasterrcnn_resnet50_fpn(num_classes=50, pretrained_backbone=False)
    model.cuda()
    model.eval()
    input_shape = (3, 300, 300)
    x = torch.rand(input_shape, device="cuda")
    model_input = [x]
    out = model(model_input)
    assert model_input[0] is x

    checkOut(out)

    with torch.cuda.amp.autocast():
        out = model(model_input)

    checkOut(out)

    _check_input_backprop(model, model_input)

    # now switch to cpu and make sure it works
    model.cpu()
    x = x.cpu()
    out_cpu = model([x])

    checkOut(out_cpu)

    _check_input_backprop(model, [x])


def test_generalizedrcnn_transform_repr():

    min_size, max_size = 224, 299
    image_mean = [0.485, 0.456, 0.406]
    image_std = [0.229, 0.224, 0.225]

    t = models.detection.transform.GeneralizedRCNNTransform(
        min_size=min_size, max_size=max_size, image_mean=image_mean, image_std=image_std
    )

    # Check integrity of object __repr__ attribute
    expected_string = "GeneralizedRCNNTransform("
    _indent = "\n    "
    expected_string += f"{_indent}Normalize(mean={image_mean}, std={image_std})"
    expected_string += f"{_indent}Resize(min_size=({min_size},), max_size={max_size}, "
    expected_string += "mode='bilinear')\n)"
    assert t.__repr__() == expected_string


@pytest.mark.parametrize("model_fn", get_models_from_module(models))
@pytest.mark.parametrize("dev", cpu_and_gpu())
def test_classification_model(model_fn, dev):
    set_rng_seed(0)
    defaults = {
        "num_classes": 50,
        "input_shape": (1, 3, 224, 224),
    }
    model_name = model_fn.__name__
    kwargs = {**defaults, **_model_params.get(model_name, {})}
    input_shape = kwargs.pop("input_shape")

    model = model_fn(**kwargs)
    model.eval().to(device=dev)
    # RNG always on CPU, to ensure x in cuda tests is bitwise identical to x in cpu tests
    x = torch.rand(input_shape).to(device=dev)
    out = model(x)
    _assert_expected(out.cpu(), model_name, prec=0.1)
    assert out.shape[-1] == 50
    _check_jit_scriptable(model, (x,), unwrapper=script_model_unwrapper.get(model_name, None))
    _check_fx_compatible(model, x)

    if dev == torch.device("cuda"):
        with torch.cuda.amp.autocast():
            out = model(x)
            # See autocast_flaky_numerics comment at top of file.
            if model_name not in autocast_flaky_numerics:
                _assert_expected(out.cpu(), model_name, prec=0.1)
            assert out.shape[-1] == 50

    _check_input_backprop(model, x)


@pytest.mark.parametrize("model_fn", get_models_from_module(models.segmentation))
@pytest.mark.parametrize("dev", cpu_and_gpu())
def test_segmentation_model(model_fn, dev):
    set_rng_seed(0)
    defaults = {
        "num_classes": 10,
        "pretrained_backbone": False,
        "input_shape": (1, 3, 32, 32),
    }
    model_name = model_fn.__name__
    kwargs = {**defaults, **_model_params.get(model_name, {})}
    input_shape = kwargs.pop("input_shape")

    model = model_fn(**kwargs)
    model.eval().to(device=dev)
    # RNG always on CPU, to ensure x in cuda tests is bitwise identical to x in cpu tests
    x = torch.rand(input_shape).to(device=dev)
    out = model(x)["out"]

    def check_out(out):
        prec = 0.01
        try:
            # We first try to assert the entire output if possible. This is not
            # only the best way to assert results but also handles the cases
            # where we need to create a new expected result.
            _assert_expected(out.cpu(), model_name, prec=prec)
        except AssertionError:
            # Unfortunately some segmentation models are flaky with autocast
            # so instead of validating the probability scores, check that the class
            # predictions match.
            expected_file = _get_expected_file(model_name)
            expected = torch.load(expected_file)
            torch.testing.assert_close(out.argmax(dim=1), expected.argmax(dim=1), rtol=prec, atol=prec)
            return False  # Partial validation performed

        return True  # Full validation performed

    full_validation = check_out(out)

    _check_jit_scriptable(model, (x,), unwrapper=script_model_unwrapper.get(model_name, None))
    _check_fx_compatible(model, x)

    if dev == torch.device("cuda"):
        with torch.cuda.amp.autocast():
            out = model(x)["out"]
            # See autocast_flaky_numerics comment at top of file.
            if model_name not in autocast_flaky_numerics:
                full_validation &= check_out(out)

    if not full_validation:
        msg = (
            f"The output of {test_segmentation_model.__name__} could only be partially validated. "
            "This is likely due to unit-test flakiness, but you may "
            "want to do additional manual checks if you made "
            "significant changes to the codebase."
        )
        warnings.warn(msg, RuntimeWarning)
        pytest.skip(msg)

    _check_input_backprop(model, x)


@pytest.mark.parametrize("model_fn", get_models_from_module(models.detection))
@pytest.mark.parametrize("dev", cpu_and_gpu())
def test_detection_model(model_fn, dev):
    set_rng_seed(0)
    defaults = {
        "num_classes": 50,
        "pretrained_backbone": False,
        "input_shape": (3, 300, 300),
    }
    model_name = model_fn.__name__
    kwargs = {**defaults, **_model_params.get(model_name, {})}
    input_shape = kwargs.pop("input_shape")

    model = model_fn(**kwargs)
    model.eval().to(device=dev)
    # RNG always on CPU, to ensure x in cuda tests is bitwise identical to x in cpu tests
    x = torch.rand(input_shape).to(device=dev)
    model_input = [x]
    out = model(model_input)
    assert model_input[0] is x

    def check_out(out):
        assert len(out) == 1

        def compact(tensor):
            size = tensor.size()
            elements_per_sample = functools.reduce(operator.mul, size[1:], 1)
            if elements_per_sample > 30:
                return compute_mean_std(tensor)
            else:
                return subsample_tensor(tensor)

        def subsample_tensor(tensor):
            num_elems = tensor.size(0)
            num_samples = 20
            if num_elems <= num_samples:
                return tensor

            ith_index = num_elems // num_samples
            return tensor[ith_index - 1 :: ith_index]

        def compute_mean_std(tensor):
            # can't compute mean of integral tensor
            tensor = tensor.to(torch.double)
            mean = torch.mean(tensor)
            std = torch.std(tensor)
            return {"mean": mean, "std": std}

        output = map_nested_tensor_object(out, tensor_map_fn=compact)
        prec = 0.01
        try:
            # We first try to assert the entire output if possible. This is not
            # only the best way to assert results but also handles the cases
            # where we need to create a new expected result.
            _assert_expected(output, model_name, prec=prec)
        except AssertionError:
            # Unfortunately detection models are flaky due to the unstable sort
            # in NMS. If matching across all outputs fails, use the same approach
            # as in NMSTester.test_nms_cuda to see if this is caused by duplicate
            # scores.
            expected_file = _get_expected_file(model_name)
            expected = torch.load(expected_file)
            torch.testing.assert_close(
                output[0]["scores"], expected[0]["scores"], rtol=prec, atol=prec, check_device=False, check_dtype=False
            )

            # Note: Fmassa proposed turning off NMS by adapting the threshold
            # and then using the Hungarian algorithm as in DETR to find the
            # best match between output and expected boxes and eliminate some
            # of the flakiness. Worth exploring.
            return False  # Partial validation performed

        return True  # Full validation performed

    full_validation = check_out(out)
    _check_jit_scriptable(model, ([x],), unwrapper=script_model_unwrapper.get(model_name, None))

    if dev == torch.device("cuda"):
        with torch.cuda.amp.autocast():
            out = model(model_input)
            # See autocast_flaky_numerics comment at top of file.
            if model_name not in autocast_flaky_numerics:
                full_validation &= check_out(out)

    if not full_validation:
        msg = (
            f"The output of {test_detection_model.__name__} could only be partially validated. "
            "This is likely due to unit-test flakiness, but you may "
            "want to do additional manual checks if you made "
            "significant changes to the codebase."
        )
        warnings.warn(msg, RuntimeWarning)
        pytest.skip(msg)

    _check_input_backprop(model, model_input)


@pytest.mark.parametrize("model_fn", get_models_from_module(models.detection))
def test_detection_model_validation(model_fn):
    set_rng_seed(0)
    model = model_fn(num_classes=50, pretrained_backbone=False)
    input_shape = (3, 300, 300)
    x = [torch.rand(input_shape)]

    # validate that targets are present in training
    with pytest.raises(ValueError):
        model(x)

    # validate type
    targets = [{"boxes": 0.0}]
    with pytest.raises(ValueError):
        model(x, targets=targets)

    # validate boxes shape
    for boxes in (torch.rand((4,)), torch.rand((1, 5))):
        targets = [{"boxes": boxes}]
        with pytest.raises(ValueError):
            model(x, targets=targets)

    # validate that no degenerate boxes are present
    boxes = torch.tensor([[1, 3, 1, 4], [2, 4, 3, 4]])
    targets = [{"boxes": boxes}]
    with pytest.raises(ValueError):
        model(x, targets=targets)


@pytest.mark.parametrize("model_fn", get_models_from_module(models.video))
@pytest.mark.parametrize("dev", cpu_and_gpu())
def test_video_model(model_fn, dev):
    # the default input shape is
    # bs * num_channels * clip_len * h *w
    input_shape = (1, 3, 4, 112, 112)
    model_name = model_fn.__name__
    # test both basicblock and Bottleneck
    model = model_fn(num_classes=50)
    model.eval().to(device=dev)
    # RNG always on CPU, to ensure x in cuda tests is bitwise identical to x in cpu tests
    x = torch.rand(input_shape).to(device=dev)
    out = model(x)
    _check_jit_scriptable(model, (x,), unwrapper=script_model_unwrapper.get(model_name, None))
    _check_fx_compatible(model, x)
    assert out.shape[-1] == 50

    if dev == torch.device("cuda"):
        with torch.cuda.amp.autocast():
            out = model(x)
            assert out.shape[-1] == 50

    _check_input_backprop(model, x)


@pytest.mark.skipif(
    not (
        "fbgemm" in torch.backends.quantized.supported_engines
        and "qnnpack" in torch.backends.quantized.supported_engines
    ),
    reason="This Pytorch Build has not been built with fbgemm and qnnpack",
)
@pytest.mark.parametrize("model_fn", get_models_from_module(models.quantization))
def test_quantized_classification_model(model_fn):
    set_rng_seed(0)
    defaults = {
        "num_classes": 5,
        "input_shape": (1, 3, 224, 224),
        "pretrained": False,
        "quantize": True,
    }
    model_name = model_fn.__name__
    kwargs = {**defaults, **_model_params.get(model_name, {})}
    input_shape = kwargs.pop("input_shape")

    # First check if quantize=True provides models that can run with input data
    model = model_fn(**kwargs)
    model.eval()
    x = torch.rand(input_shape)
    out = model(x)

    if model_name not in quantized_flaky_models:
        _assert_expected(out, model_name + "_quantized", prec=0.1)
        assert out.shape[-1] == 5
        _check_jit_scriptable(model, (x,), unwrapper=script_model_unwrapper.get(model_name, None))
        _check_fx_compatible(model, x)

    kwargs["quantize"] = False
    for eval_mode in [True, False]:
        model = model_fn(**kwargs)
        if eval_mode:
            model.eval()
            model.qconfig = torch.quantization.default_qconfig
        else:
            model.train()
            model.qconfig = torch.quantization.default_qat_qconfig

        model.fuse_model()
        if eval_mode:
            torch.quantization.prepare(model, inplace=True)
        else:
            torch.quantization.prepare_qat(model, inplace=True)
            model.eval()

        torch.quantization.convert(model, inplace=True)

    try:
        torch.jit.script(model)
    except Exception as e:
        tb = traceback.format_exc()
        raise AssertionError(f"model cannot be scripted. Traceback = {str(tb)}") from e


@pytest.mark.parametrize("model_fn", get_models_from_module(models.detection))
def test_detection_model_trainable_backbone_layers(model_fn, disable_weight_loading):
    model_name = model_fn.__name__
    max_trainable = _model_tests_values[model_name]["max_trainable"]
    n_trainable_params = []
    for trainable_layers in range(0, max_trainable + 1):
        model = model_fn(pretrained=False, pretrained_backbone=True, trainable_backbone_layers=trainable_layers)

        n_trainable_params.append(len([p for p in model.parameters() if p.requires_grad]))
    assert n_trainable_params == _model_tests_values[model_name]["n_trn_params_per_layer"]


if __name__ == "__main__":
    pytest.main([__file__])
