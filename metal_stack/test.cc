#include <cuda_runtime.h>
#include "cublas_v2.h"

#include "thin-stack.h"
#include "util.h"


ThinStackParameters load_params(ModelSpec spec) {
  float *compose_W_l = load_weights_cuda("params/compose_W_l.txt",
      spec.model_dim * spec.model_dim);
  float *compose_W_r = load_weights_cuda("params/compose_W_r.txt",
      spec.model_dim * spec.model_dim);

  ThinStackParameters ret = {
    NULL, NULL, // projection
    NULL, NULL, // buffer batch-norm
    NULL, NULL, NULL, // tracking
    compose_W_l, compose_W_r, NULL, NULL // composition
  };

  return ret;
}

int main() {
  ModelSpec spec = {5, 5, 2, 10, 3, 5};
  ThinStackParameters params = load_params(spec);

  cublasHandle_t handle;
  cublasStatus_t stat = cublasCreate(&handle);
  if (stat != CUBLAS_STATUS_SUCCESS) {
    printf("CUBLAS initialization failed\n");
    return 1;
  }

  ThinStack ts(spec, params, handle);
}
