#ifndef _thin_stack_
#define _thin_stack_

#include <cuda_runtime.h>
#include "cublas_v2.h"

#include "util.h"

#include "kernels.cuh"


typedef struct ThinStackParameters {
  float *project_W;
  float *project_b;
  float *buffer_bn_ts;
  float *buffer_bn_tm;
  float *tracking_W_inp;
  float *tracking_W_hid;
  float *tracking_b;
  float *compose_W_l;
  float *compose_W_r;
  float *compose_W_ext;
  float *compose_b;
} ThinStackParameters;

class ThinStack {
  public:
    /**
     * Constructs a new `ThinStack`.
     */
    ThinStack(ModelSpec spec, ThinStackParameters params,
            cublasHandle_t handle);

    ~ThinStack();

    void forward();

    // Embedding inputs, of dimension `model_dim * (batch_size * seq_length)` --
    // i.e., along 2nd axis we have `seq_length`-many `model_dim * batch_size`
    // matrices.
    float *X;
    float *transitions;

    float *stack;

  private:

    void step(int t);

    // Reset internal storage. Must be run before beginning a sequence
    // feedforward.
    void reset();

    void recurrence(const float *stack_1_t, const float *stack_2_t,
            const float *buffer_top_t);
    void mask_and_update_stack(const float *push_value,
            const float *merge_value, const float *transitions, int t);
    void mask_and_update_cursors(float *cursors, const float *transitions,
                                 int t);
    void update_buffer_cur(float *buffer_cur_t, float *transitions, int t);

    void init_helpers();
    void free_helpers();

    ModelSpec spec;
    ThinStackParameters params;
    cublasHandle_t handle;

    size_t stack_size;

    size_t stack_total_size;
    size_t buffer_total_size;
    size_t queue_total_size;
    size_t cursors_total_size;

    // Containers for temporary (per-step) data
    float *buffer_top_idxs_t;
    float *buffer_top_t;
    float *stack_1_ptrs;
    float *stack_1_t;
    float *stack_2_ptrs;
    float *stack_2_t;
    float *push_output;
    float *merge_output;

    // Per-step accumulators
    float *buffer_cur_t;

    // Dumb helpers
    float *batch_ones;
    float *batch_range;

    // `model_dim * (batch_size * seq_length)`
    // `seq_length`-many `model_dim * batch_size` matrices, flattened into one.
    float *buffer;
    float *queue;
    float *cursors;

};

#endif
