#include "thin-stack.h"
using namespace std;
namespace k = kernels;


ThinStack::ThinStack(ModelSpec spec, ThinStackParameters params,
    cublasHandle_t handle)
  : spec(spec), params(params), stack_size(spec.seq_length), handle(handle) {

  stack_total_size = (stack_size * spec.batch_size) * spec.model_dim;
  buffer_total_size = spec.batch_size * spec.seq_length * spec.model_dim;
  queue_total_size = spec.batch_size * spec.seq_length;
  cursors_total_size = spec.batch_size;

  // Pre-allocate inputs.
  cudaMalloc(&X, spec.batch_size * spec.seq_length * spec.model_dim * sizeof(float));
  cudaMalloc(&transitions, spec.batch_size * spec.seq_length * sizeof(float));

  // Pre-allocate auxiliary data structures.
  cudaMalloc(&stack, stack_total_size * sizeof(float));
  cudaMalloc(&queue, queue_total_size * sizeof(float));
  cudaMalloc(&cursors, cursors_total_size * sizeof(float));
  cudaMalloc(&buffer, buffer_total_size * sizeof(float));

  // Pre-allocate temporary containers.
  cudaMalloc(&buffer_top_idxs_t, spec.batch_size * sizeof(float));
  cudaMalloc(&buffer_top_t, spec.batch_size * spec.model_dim * sizeof(float));
  cudaMalloc(&stack_1_ptrs, spec.batch_size * sizeof(float));
  cudaMalloc(&stack_1_t, spec.model_dim * spec.batch_size * sizeof(float));
  cudaMalloc(&stack_2_ptrs, spec.batch_size * sizeof(float));
  cudaMalloc(&stack_2_t, spec.model_dim * spec.batch_size * sizeof(float));
  cudaMalloc(&push_output, spec.batch_size * spec.model_dim * sizeof(float));
  cudaMalloc(&merge_output, spec.batch_size * spec.model_dim * sizeof(float));

  // Pre-allocate accumulators.
  cudaMalloc(&buffer_cur_t, spec.batch_size * sizeof(float));

  init_helpers();

}


void ThinStack::init_helpers() {
  cudaMalloc(&batch_range, spec.batch_size * sizeof(float));
  cudaMalloc(&batch_ones, spec.batch_size * sizeof(float));

  float h_batch_ones[spec.batch_size];
  float h_batch_range[spec.batch_size];
  for (int i = 0; i < spec.batch_size; i++) {
    h_batch_ones[i] = 1.0f;
    h_batch_range[i] = (float) i;
  }

  cudaMemcpy(batch_range, h_batch_range, spec.batch_size * sizeof(float),
      cudaMemcpyHostToDevice);
  cudaMemcpy(batch_ones, h_batch_ones, spec.batch_size * sizeof(float),
      cudaMemcpyHostToDevice);
  cudaDeviceSynchronize();
}


void ThinStack::free_helpers() {
  cudaFree(batch_ones);
  cudaFree(batch_range);
}


ThinStack::~ThinStack() {

  free_helpers();

  cudaFree(X);
  cudaFree(transitions);

  cudaFree(stack);
  cudaFree(queue);
  cudaFree(cursors);
  cudaFree(buffer);

  cudaFree(buffer_top_idxs_t);
  cudaFree(buffer_top_t);
  cudaFree(stack_1_ptrs);
  cudaFree(stack_2_ptrs);
  cudaFree(push_output);
  cudaFree(merge_output);

  cudaFree(buffer_cur_t);

}


void ThinStack::forward() {

  // TODO embedding projection
  buffer = X;
  zero();

  for (int t = 0; t < spec.seq_length; t++) {
    step(t);
    cout << endl << "======================" << endl << endl;
  }

}


void ThinStack::step(int t) {

  // TODO sync after kernel calls.
  float *transitions_t = &transitions[t * spec.batch_size];

  // buffer_top = buffer[buffer_cur_t * batch_size + batch_range]
  //cout << "buffer_top before:" << endl;
  //print_device_matrix(buffer_top_t, spec.model_dim, spec.batch_size);
  k::subtensor1(buffer_top_t, buffer, buffer_cur_t,
          spec.batch_size * spec.model_dim, spec.batch_size,
          spec.model_dim, 0.0f, spec.batch_size, 1.0f, batch_range);
  cout << "buffer_top after:" << endl;
  print_device_matrix(buffer_top_t, spec.model_dim, spec.batch_size);

  // stack_2_ptrs = (cursors - 1) + batch_range * seq_length
  k::subtensor1(stack_2_ptrs, queue, cursors, spec.batch_size,
          spec.batch_size, spec.seq_length, -1.0f, 1.0f, spec.seq_length,
          batch_range);
  cout << "stack_2_ptrs #1" << endl;
  print_device_matrix(stack_2_ptrs, 1, spec.batch_size);

  // stack_2_ptrs = stack_2_ptrs * batch_size + batch_range * 1
  k::addi_vv(handle, stack_2_ptrs, batch_range, spec.batch_size, 1,
          spec.batch_size);
  cout << "stack_2_ptrs" << endl;
  print_device_matrix(stack_2_ptrs, 1, spec.batch_size);

  // stack_1, stack_2
  // stack_1_t = stack[batch_range + (t - 1) * spec.batch_size]
  k::subtensor1(stack_1_t, stack, batch_range,
          spec.batch_size * spec.seq_length, spec.batch_size,
          spec.model_dim, (float) (t - 1) * spec.batch_size, 1.0f, 0.0f,
          NULL);

  k::subtensor1(stack_2_t, stack, stack_2_ptrs,
          spec.batch_size * spec.seq_length, spec.batch_size,
          spec.model_dim, 0.0f, 1.0f, 0.0f, NULL);

  // Run recurrence, which writes into `push_output`, `merge_output`.
  recurrence(stack_1_t, stack_2_t, buffer_top_t);

  // Write in the next stack top.
  mask_and_update_stack(buffer_top_t, merge_output, transitions_t, t);

  mask_and_update_cursors(cursors, transitions_t, t);

  // queue[cursors + 0 + batch_range * spec.seq_length] = t
  k::set_subtensor1i_s(queue, t, cursors, spec.batch_size, 0, spec.seq_length,
          batch_range);
  cout << "queue after" << endl;
  print_device_matrix(queue, spec.seq_length, spec.batch_size);

  // buffer_cur += (1 - transitions)
  update_buffer_cur(buffer_cur_t, transitions_t, t);
  cout << "buffer cur after" << endl;
  print_device_matrix(buffer_cur_t, 1, spec.batch_size);

}


void ThinStack::recurrence(const float *stack_1_t, const float *stack_2_t,
    const float *buffer_top_t) {

  // merge_out = W_l l
  float alpha = 1.0f;
  float beta = 0.0f;
  cublasSgemm(handle, CUBLAS_OP_N, CUBLAS_OP_N, spec.model_dim, spec.batch_size,
      spec.model_dim, &alpha, params.compose_W_l, spec.model_dim, stack_2_t,
      spec.model_dim, &beta, merge_output, spec.model_dim);
  // merge_out += W_r r
  float beta2 = 1.0f;
  auto ret = cublasSgemm(handle, CUBLAS_OP_N, CUBLAS_OP_N, spec.model_dim, spec.batch_size,
      spec.model_dim, &alpha, params.compose_W_r, spec.model_dim, stack_1_t,
      spec.model_dim, &beta2, merge_output, spec.model_dim);

  // TODO bias (broadcast-add)
  // TODO relu

}


void ThinStack::mask_and_update_stack(const float *push_value,
    const float *merge_value, const float *transitions, int t) {

  // Find start position of write destination (next-top corresponding to
  // timestep `t`).
  int stack_offset = t * spec.batch_size * spec.model_dim;

  cout << "merge value:" << endl;
  print_device_matrix(merge_value, spec.model_dim, spec.batch_size);

  cout << "push value:" << endl;
  print_device_matrix(push_value, spec.model_dim, spec.batch_size);

  k::switch_m(&stack[stack_offset], transitions, merge_value, push_value,
              spec.batch_size, spec.model_dim);

  cout << "stack top t:" << endl;
  print_device_matrix(&stack[stack_offset], spec.model_dim, spec.batch_size);

}


void ThinStack::mask_and_update_cursors(float *cursors, const float *transitions,
    int t) {

  // cursors += 1
  float alpha1 = 1.0f;
  cublasSaxpy(handle, spec.batch_size, &alpha1, batch_ones, 1, cursors, 1);

  // cursors -= 2*transitions
  float alpha2 = -2.0f;
  cublasSaxpy(handle, spec.batch_size, &alpha2, transitions, 1, cursors, 1);

}


void ThinStack::update_buffer_cur(float *buffer_cur_t, float *transitions, int t) {

  // buffer_cur += 1
  float alpha1 = 1.0;
  cublasSaxpy(handle, spec.batch_size, &alpha1, batch_ones, 1, buffer_cur_t, 1);

  // buffer_cur -= transitions
  float alpha2 = -1.0;
  cublasSaxpy(handle, spec.batch_size, &alpha2, transitions, 1, buffer_cur_t, 1);

}


void ThinStack::zero() {
  cudaMemset(stack, 0, stack_total_size * sizeof(float));
  cudaMemset(queue, 0, queue_total_size * sizeof(float));
  cudaMemset(cursors, 0, cursors_total_size * sizeof(float));

  cudaMemset(buffer_cur_t, 0, spec.batch_size * sizeof(float));
}
