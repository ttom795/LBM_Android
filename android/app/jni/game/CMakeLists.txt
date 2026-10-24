# The game: host runtime + recompiled code from port/runtime and port/gen.
get_filename_component(PORT ${CMAKE_CURRENT_SOURCE_DIR}/../../../.. ABSOLUTE)
file(GLOB GEN_SRC ${PORT}/gen/*.c)
file(GLOB RT_SRC ${PORT}/runtime/*.c)
file(GLOB RT_CXX ${PORT}/runtime/*.cpp)

if(GEN_SRC)
    add_library(lbgen OBJECT ${GEN_SRC})
    set_property(TARGET lbgen PROPERTY JOB_POOL_COMPILE gen)
    target_include_directories(lbgen PRIVATE ${PORT}/runtime ${PORT}/gen)
    target_compile_options(lbgen PRIVATE -std=gnu11 -O1 -g0 -fno-strict-aliasing -fwrapv -w -fPIC)

    add_library(main SHARED ${RT_SRC} ${RT_CXX} $<TARGET_OBJECTS:lbgen>)
    target_include_directories(main PRIVATE ${PORT}/runtime ${PORT}/gen ../sdlinc
        ../dxvk/include/native/directx ../dxvk/include/native/windows ../dxvk/include/native)
    target_compile_definitions(main PRIVATE DXVK_NATIVE)
    target_compile_options(main PRIVATE -O2 -fno-strict-aliasing -fwrapv -Wno-everything
        $<$<COMPILE_LANGUAGE:C>:-std=gnu11>)
    target_link_libraries(main PRIVATE SDL2 dxvk_d3d9 m dl $<$<BOOL:${ANDROID}>:log>)
elseif(EXISTS ${PORT}/android/app/src/main/jniLibs/arm64-v8a/libmain.so)
    message(STATUS "Using prebuilt libmain.so in jniLibs/arm64-v8a")
else()
    message(WARNING "gen/*.c not found and no prebuilt libmain.so found. Please generate gen/ via tools/regen_gen.sh")
endif()
