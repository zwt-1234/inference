# Copyright 2022-2023 XProbe Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import collections.abc
import logging
import os
import platform
from collections import defaultdict
from typing import Dict, List, Literal, Optional, Tuple, Union

from ...constants import XINFERENCE_CACHE_DIR
from ...types import PeftModelConfig
from ..core import CacheableModelSpec, ModelDescription, VirtualEnvSettings
from ..utils import (
    IS_NEW_HUGGINGFACE_HUB,
    retry_download,
    symlink_local_file,
    valid_model_revision,
)
from .ocr.got_ocr2 import GotOCR2Model
from .stable_diffusion.core import DiffusionModel
from .stable_diffusion.mlx import MLXDiffusionModel

logger = logging.getLogger(__name__)

MODEL_NAME_TO_REVISION: Dict[str, List[str]] = defaultdict(list)
IMAGE_MODEL_DESCRIPTIONS: Dict[str, List[Dict]] = defaultdict(list)
BUILTIN_IMAGE_MODELS: Dict[str, "ImageModelFamilyV1"] = {}
MODELSCOPE_IMAGE_MODELS: Dict[str, "ImageModelFamilyV1"] = {}


def get_image_model_descriptions():
    import copy

    return copy.deepcopy(IMAGE_MODEL_DESCRIPTIONS)


class ImageModelFamilyV1(CacheableModelSpec):
    model_family: str
    model_name: str
    model_id: str
    model_revision: str
    model_hub: str = "huggingface"
    model_ability: Optional[List[str]]
    controlnet: Optional[List["ImageModelFamilyV1"]]
    default_model_config: Optional[dict] = {}
    default_generate_config: Optional[dict] = {}
    gguf_model_id: Optional[str]
    gguf_quantizations: Optional[List[str]]
    gguf_model_file_name_template: Optional[str]
    virtualenv: Optional[VirtualEnvSettings]


class ImageModelDescription(ModelDescription):
    def __init__(
        self,
        address: Optional[str],
        devices: Optional[List[str]],
        model_spec: ImageModelFamilyV1,
        model_path: Optional[str] = None,
    ):
        super().__init__(address, devices, model_path=model_path)
        self._model_spec = model_spec

    @property
    def spec(self):
        return self._model_spec

    def to_dict(self):
        if self._model_spec.controlnet is not None:
            controlnet = [cn.dict() for cn in self._model_spec.controlnet]
        else:
            controlnet = self._model_spec.controlnet
        return {
            "model_type": "image",
            "address": self.address,
            "accelerators": self.devices,
            "model_name": self._model_spec.model_name,
            "model_family": self._model_spec.model_family,
            "model_revision": self._model_spec.model_revision,
            "model_ability": self._model_spec.model_ability,
            "controlnet": controlnet,
        }

    def to_version_info(self):
        from .utils import get_model_version

        if self._model_path is None:
            is_cached = get_cache_status(self._model_spec)
            file_location = get_cache_dir(self._model_spec)
        else:
            is_cached = True
            file_location = self._model_path

        if self._model_spec.controlnet is None:
            return [
                {
                    "model_version": get_model_version(self._model_spec, None),
                    "model_file_location": file_location,
                    "cache_status": is_cached,
                    "controlnet": "zoe-depth",
                }
            ]
        else:
            res = []
            for cn in self._model_spec.controlnet:
                res.append(
                    {
                        "model_version": get_model_version(self._model_spec, cn),
                        "model_file_location": file_location,
                        "cache_status": is_cached,
                        "controlnet": cn.model_name,
                    }
                )
            return res


def generate_image_description(
    image_model: ImageModelFamilyV1,
) -> Dict[str, List[Dict]]:
    res = defaultdict(list)
    res[image_model.model_name].extend(
        ImageModelDescription(None, None, image_model).to_version_info()
    )
    return res


def match_diffusion(
    model_name: str,
    download_hub: Optional[
        Literal["huggingface", "modelscope", "openmind_hub", "csghub"]
    ] = None,
) -> ImageModelFamilyV1:
    from ..utils import download_from_modelscope
    from . import BUILTIN_IMAGE_MODELS, MODELSCOPE_IMAGE_MODELS
    from .custom import get_user_defined_images

    for model_spec in get_user_defined_images():
        if model_spec.model_name == model_name:
            return model_spec

    if download_hub == "modelscope" and model_name in MODELSCOPE_IMAGE_MODELS:
        logger.debug(f"Image model {model_name} found in ModelScope.")
        return MODELSCOPE_IMAGE_MODELS[model_name]
    elif download_hub == "huggingface" and model_name in BUILTIN_IMAGE_MODELS:
        logger.debug(f"Image model {model_name} found in Huggingface.")
        return BUILTIN_IMAGE_MODELS[model_name]
    elif download_from_modelscope() and model_name in MODELSCOPE_IMAGE_MODELS:
        logger.debug(f"Image model {model_name} found in ModelScope.")
        return MODELSCOPE_IMAGE_MODELS[model_name]
    elif model_name in BUILTIN_IMAGE_MODELS:
        logger.debug(f"Image model {model_name} found in Huggingface.")
        return BUILTIN_IMAGE_MODELS[model_name]
    else:
        raise ValueError(
            f"Image model {model_name} not found, available"
            f"model list: {BUILTIN_IMAGE_MODELS.keys()}"
        )


def cache(model_spec: ImageModelFamilyV1):
    from ..utils import cache

    return cache(model_spec, ImageModelDescription)


def get_cache_dir(model_spec: ImageModelFamilyV1):
    return os.path.realpath(os.path.join(XINFERENCE_CACHE_DIR, model_spec.model_name))


def get_cache_status(
    model_spec: ImageModelFamilyV1,
) -> bool:
    cache_dir = get_cache_dir(model_spec)
    meta_path = os.path.join(cache_dir, "__valid_download")

    model_name = model_spec.model_name
    if model_name in BUILTIN_IMAGE_MODELS and model_name in MODELSCOPE_IMAGE_MODELS:
        hf_spec = BUILTIN_IMAGE_MODELS[model_name]
        ms_spec = MODELSCOPE_IMAGE_MODELS[model_name]

        return any(
            [
                valid_model_revision(meta_path, hf_spec.model_revision),
                valid_model_revision(meta_path, ms_spec.model_revision),
            ]
        )
    else:  # Usually for UT
        return valid_model_revision(meta_path, model_spec.model_revision)


def cache_gguf(spec: ImageModelFamilyV1, quantization: Optional[str] = None):
    if not quantization:
        return

    cache_dir = os.path.realpath(os.path.join(XINFERENCE_CACHE_DIR, spec.model_name))
    if not os.path.exists(cache_dir):
        os.makedirs(cache_dir, exist_ok=True)

    if not spec.gguf_model_file_name_template:
        raise NotImplementedError(
            f"{spec.model_name} does not support GGUF quantization"
        )
    if quantization not in (spec.gguf_quantizations or []):
        raise ValueError(
            f"Cannot support quantization {quantization}, "
            f"available quantizations: {spec.gguf_quantizations}"
        )

    filename = spec.gguf_model_file_name_template.format(quantization=quantization)  # type: ignore
    full_path = os.path.join(cache_dir, filename)

    if spec.model_hub == "huggingface":
        import huggingface_hub

        use_symlinks = {}
        if not IS_NEW_HUGGINGFACE_HUB:
            use_symlinks = {"local_dir_use_symlinks": True, "local_dir": cache_dir}
        download_file_path = retry_download(
            huggingface_hub.hf_hub_download,
            spec.model_name,
            None,
            spec.gguf_model_id,
            filename=filename,
            **use_symlinks,
        )
        if IS_NEW_HUGGINGFACE_HUB:
            symlink_local_file(download_file_path, cache_dir, filename)
    elif spec.model_hub == "modelscope":
        from modelscope.hub.file_download import model_file_download

        download_file_path = retry_download(
            model_file_download,
            spec.model_name,
            None,
            spec.gguf_model_id,
            filename,
            revision=spec.model_revision,
        )
        symlink_local_file(download_file_path, cache_dir, filename)
    else:
        raise NotImplementedError

    return full_path


def create_ocr_model_instance(
    subpool_addr: str,
    devices: List[str],
    model_uid: str,
    model_spec: ImageModelFamilyV1,
    model_path: Optional[str] = None,
    **kwargs,
) -> Tuple[GotOCR2Model, ImageModelDescription]:
    if not model_path:
        model_path = cache(model_spec)
    model = GotOCR2Model(
        model_uid,
        model_path,
        model_spec=model_spec,
        **kwargs,
    )
    model_description = ImageModelDescription(
        subpool_addr, devices, model_spec, model_path=model_path
    )
    return model, model_description


def create_image_model_instance(
    subpool_addr: str,
    devices: List[str],
    model_uid: str,
    model_name: str,
    peft_model_config: Optional[PeftModelConfig] = None,
    download_hub: Optional[
        Literal["huggingface", "modelscope", "openmind_hub", "csghub"]
    ] = None,
    model_path: Optional[str] = None,
    gguf_quantization: Optional[str] = None,
    gguf_model_path: Optional[str] = None,
    **kwargs,
) -> Tuple[
    Union[DiffusionModel, MLXDiffusionModel, GotOCR2Model], ImageModelDescription
]:
    model_spec = match_diffusion(model_name, download_hub)
    if model_spec.model_ability and "ocr" in model_spec.model_ability:
        return create_ocr_model_instance(
            subpool_addr=subpool_addr,
            devices=devices,
            model_uid=model_uid,
            model_name=model_name,
            model_spec=model_spec,
            model_path=model_path,
            **kwargs,
        )

    # use default model config
    model_default_config = (model_spec.default_model_config or {}).copy()
    model_default_config.update(kwargs)
    kwargs = model_default_config

    controlnet = kwargs.get("controlnet")
    # Handle controlnet
    if controlnet is not None:
        if isinstance(controlnet, str):
            controlnet = [controlnet]
        elif not isinstance(controlnet, collections.abc.Sequence):
            raise ValueError("controlnet should be a str or a list of str.")
        elif set(controlnet) != len(controlnet):
            raise ValueError("controlnet should be a list of unique str.")
        elif not model_spec.controlnet:
            raise ValueError(f"Model {model_name} has empty controlnet list.")

        controlnet_model_paths = []
        assert model_spec.controlnet is not None
        for name in controlnet:
            for cn_model_spec in model_spec.controlnet:
                if cn_model_spec.model_name == name:
                    controlnet_model_path = cache(cn_model_spec)
                    controlnet_model_paths.append(controlnet_model_path)
                    break
            else:
                raise ValueError(
                    f"controlnet `{name}` is not supported for model `{model_name}`."
                )
        if len(controlnet_model_paths) == 1:
            kwargs["controlnet"] = (controlnet[0], controlnet_model_paths[0])
        else:
            kwargs["controlnet"] = [
                (n, path) for n, path in zip(controlnet, controlnet_model_paths)
            ]
    if not model_path:
        model_path = cache(model_spec)
    if not gguf_model_path and gguf_quantization:
        gguf_model_path = cache_gguf(model_spec, gguf_quantization)
    if peft_model_config is not None:
        lora_model = peft_model_config.peft_model
        lora_load_kwargs = peft_model_config.image_lora_load_kwargs
        lora_fuse_kwargs = peft_model_config.image_lora_fuse_kwargs
    else:
        lora_model = None
        lora_load_kwargs = None
        lora_fuse_kwargs = None

    if (
        platform.system() == "Darwin"
        and "arm" in platform.machine().lower()
        and model_name in MLXDiffusionModel.supported_models
    ):
        # Mac with M series silicon chips
        model_cls = MLXDiffusionModel
    else:
        model_cls = DiffusionModel  # type: ignore

    model = model_cls(
        model_uid,
        model_path,
        lora_model=lora_model,
        lora_load_kwargs=lora_load_kwargs,
        lora_fuse_kwargs=lora_fuse_kwargs,
        model_spec=model_spec,
        gguf_model_path=gguf_model_path,
        **kwargs,
    )
    model_description = ImageModelDescription(
        subpool_addr, devices, model_spec, model_path=model_path
    )
    return model, model_description
