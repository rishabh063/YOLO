import os

import torch
from loguru import logger
from torch import Tensor

from yolo.config.config import Config
from yolo.model.yolo import create_model


class FastModelLoader:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.compiler = cfg.task.fast_inference
        self._validate_compiler()
        if cfg.weight == True:
            cfg.weight = os.path.join("weights", f"{cfg.model.name}.pt")
        self.model_path = f"{os.path.splitext(cfg.weight)[0]}.{self.compiler}"

    def _validate_compiler(self):
        if self.compiler not in ["onnx", "trt", "deploy"]:
            logger.warning(f"⚠️ Compiler '{self.compiler}' is not supported. Using original model.")
            self.compiler = None
        if self.cfg.device == "mps" and self.compiler == "trt":
            logger.warning("🍎 TensorRT does not support MPS devices. Using original model.")
            self.compiler = None

    def load_model(self, device):
        if self.compiler == "onnx":
            return self._load_onnx_model()
        elif self.compiler == "trt":
            return self._load_trt_model().to(device)
        elif self.compiler == "deploy":
            self.cfg.model.model.auxiliary = {}
        return create_model(self.cfg.model, class_num=self.cfg.class_num, weight_path=self.cfg.weight).to(device)

    def _load_onnx_model(self):
        from onnxruntime import InferenceSession

        def onnx_forward(self: InferenceSession, x: Tensor):
            x = {self.get_inputs()[0].name: x.cpu().numpy()}
            model_outputs, layer_output = [], []
            for idx, predict in enumerate(self.run(None, x)):
                layer_output.append(torch.from_numpy(predict))
                if idx % 3 == 2:
                    model_outputs.append(layer_output)
                    layer_output = []
            return {"Main": model_outputs}

        InferenceSession.__call__ = onnx_forward
        try:
            ort_session = InferenceSession(self.model_path)
            logger.info("🚀 Using ONNX as MODEL frameworks!")
        except Exception as e:
            logger.warning(f"🈳 Error loading ONNX model: {e}")
            ort_session = self._create_onnx_model()
        # TODO: Update if GPU onnx unavailable change to cpu
        self.cfg.device = "cpu"
        return ort_session

    def _create_onnx_model(self):
        from onnxruntime import InferenceSession
        from torch.onnx import export

        model = create_model(self.cfg.model, class_num=self.cfg.class_num, weight_path=self.cfg.weight).eval()
        dummy_input = torch.ones((1, 3, *self.cfg.image_size))
        export(
            model,
            dummy_input,
            self.model_path,
            input_names=["input"],
            output_names=["output"],
            dynamic_axes={"input": {0: "batch_size"}, "output": {0: "batch_size"}},
        )
        logger.info(f"📥 ONNX model saved to {self.model_path}")
        return InferenceSession(self.model_path)

    def _load_trt_model(self):
        from torch2trt import TRTModule

        try:
            model_trt = TRTModule()
            model_trt.load_state_dict(torch.load(self.model_path))
            logger.info("🚀 Using TensorRT as MODEL frameworks!")
        except FileNotFoundError:
            logger.warning(f"🈳 No found model weight at {self.model_path}")
            model_trt = self._create_trt_model()
        return model_trt

    def _create_trt_model(self):
        from torch2trt import torch2trt

        model = create_model(self.cfg.model, class_num=self.cfg.class_num, weight_path=self.cfg.weight).eval()
        dummy_input = torch.ones((1, 3, *self.cfg.image_size)).cuda()
        logger.info(f"♻️ Creating TensorRT model")
        model_trt = torch2trt(model.cuda(), [dummy_input])
        torch.save(model_trt.state_dict(), self.model_path)
        logger.info(f"📥 TensorRT model saved to {self.model_path}")
        return model_trt
