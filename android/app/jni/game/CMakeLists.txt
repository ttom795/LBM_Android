# The game: host runtime + recompiled code from port/runtime and port/gen.
get_filename_component(PORT ${CMAKE_CURRENT_SOURCE_DIR}/../../../.. ABSOLUTE)
file(GLOB GEN_SRC ${PORT}/gen/*.c)
file(GLOB RT_SRC ${PORT}/runtime/*.c)
file(GLOB RT_CXX ${PORT}/runtime/*.cpp)

add_library(lbgen OBJECT ${GEN_SRC})
set_property(TARGET lbgen PROPERTY JOB_POOL_COMPILE gen)
target_include_directories(lbgen PRIVATE ${PORT}/runtime ${PORT}/gen)
# -O2 tuned for the Cortex-A73/A53 cores of Snapdragon 6xx phones (was -O1);
# -fsigned-char: x86 char is signed, ARM's is not.
target_compile_options(lbgen PRIVATE -std=gnu11 -O2 -mtune=cortex-a73 -g0 -fno-strict-aliasing -fwrapv -fsigned-char -w -fPIC)

add_library(main SHARED ${RT_SRC} ${RT_CXX} $<TARGET_OBJECTS:lbgen>)
target_include_directories(main PRIVATE ${PORT}/runtime ${PORT}/gen ../sdlinc
    ../dxvk/include/native/directx ../dxvk/include/native/windows ../dxvk/include/native)
target_compile_definitions(main PRIVATE DXVK_NATIVE)
target_compile_options(main PRIVATE -O2 -fno-strict-aliasing -fwrapv -fsigned-char -Wno-everything
    $<$<COMPILE_LANGUAGE:C>:-std=gnu11>)
target_link_libraries(main PRIVATE SDL2 dxvk_d3d9 m $<$<BOOL:${ANDROID}>:log>)
