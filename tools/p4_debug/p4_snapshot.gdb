set pagination off
set confirm off
set print pretty on

target remote :3333

monitor halt
printf "\n=== targets ===\n"
monitor targets

printf "\n=== threads ===\n"
info threads

printf "\n=== backtraces ===\n"
thread apply all bt

printf "\n=== heap ===\n"
p/x heap_caps_get_free_size(8)
p/x heap_caps_get_largest_free_block(8)
p/x heap_caps_get_minimum_free_size(8)

printf "\n=== ticks ===\n"
p/x xTaskGetTickCount()

monitor resume
detach
quit
