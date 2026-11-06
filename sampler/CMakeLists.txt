cmake_minimum_required(VERSION 3.15)
project(touchseq-sampler)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

# The same DPF checkout the XYFX plugin uses (git submodule: git submodule update --init --recursive)
set(DPF_DIR "${CMAKE_SOURCE_DIR}/../xyfx/dpf" CACHE PATH "DPF checkout")
if(NOT EXISTS "${DPF_DIR}/distrho/DistrhoPlugin.hpp")
  message(FATAL_ERROR "DPF not found at ${DPF_DIR} - run: git submodule update --init --recursive")
endif()
add_subdirectory(${DPF_DIR} dpf)

# Rubber Band 4.0.0 (GPL-2.0-or-later), vendored: the sampler's pitch shift without the length change.
# Built statically from the same sources upstream's meson build uses, with the built-in FFT and resampler,
# so the plugin carries no runtime library of its own.
set(RUBBERBAND_DIR "${CMAKE_SOURCE_DIR}/rubberband")
set(RUBBERBAND_SOURCES
  ${RUBBERBAND_DIR}/src/rubberband-c.cpp
  ${RUBBERBAND_DIR}/src/RubberBandStretcher.cpp
  ${RUBBERBAND_DIR}/src/RubberBandLiveShifter.cpp
  ${RUBBERBAND_DIR}/src/faster/AudioCurveCalculator.cpp
  ${RUBBERBAND_DIR}/src/faster/CompoundAudioCurve.cpp
  ${RUBBERBAND_DIR}/src/faster/HighFrequencyAudioCurve.cpp
  ${RUBBERBAND_DIR}/src/faster/SilentAudioCurve.cpp
  ${RUBBERBAND_DIR}/src/faster/PercussiveAudioCurve.cpp
  ${RUBBERBAND_DIR}/src/faster/R2Stretcher.cpp
  ${RUBBERBAND_DIR}/src/faster/StretcherChannelData.cpp
  ${RUBBERBAND_DIR}/src/faster/StretcherProcess.cpp
  ${RUBBERBAND_DIR}/src/common/Allocators.cpp
  ${RUBBERBAND_DIR}/src/common/BQResampler.cpp
  ${RUBBERBAND_DIR}/src/common/FFT.cpp
  ${RUBBERBAND_DIR}/src/common/Log.cpp
  ${RUBBERBAND_DIR}/src/common/Profiler.cpp
  ${RUBBERBAND_DIR}/src/common/Resampler.cpp
  ${RUBBERBAND_DIR}/src/common/StretchCalculator.cpp
  ${RUBBERBAND_DIR}/src/common/sysutils.cpp
  ${RUBBERBAND_DIR}/src/common/mathmisc.cpp
  ${RUBBERBAND_DIR}/src/common/Thread.cpp
  ${RUBBERBAND_DIR}/src/finer/R3Stretcher.cpp
  ${RUBBERBAND_DIR}/src/finer/R3LiveShifter.cpp)
add_library(touchseq-rubberband STATIC ${RUBBERBAND_SOURCES})
set_target_properties(touchseq-rubberband PROPERTIES POSITION_INDEPENDENT_CODE ON)  # linked into the .so plugins
target_include_directories(touchseq-rubberband PUBLIC "${RUBBERBAND_DIR}" "${RUBBERBAND_DIR}/src")
target_compile_definitions(touchseq-rubberband PRIVATE
  USE_BUILTIN_FFT USE_BQRESAMPLER USE_PTHREADS HAVE_POSIX_MEMALIGN
  NO_THREAD_CHECKS NO_TIMING NDEBUG)
target_compile_options(touchseq-rubberband PRIVATE -O3)
find_package(Threads REQUIRED)
target_link_libraries(touchseq-rubberband PUBLIC Threads::Threads)

dpf_add_plugin(touchseq-sampler
  TARGETS lv2 clap
  FILES_DSP SamplerPlugin.cpp SamplerCore.cpp)
target_include_directories(touchseq-sampler PUBLIC ".")
target_link_libraries(touchseq-sampler PRIVATE touchseq-rubberband)

# DSP test: ./sampler-test
add_executable(sampler-test test.cpp SamplerCore.cpp)
target_include_directories(sampler-test PRIVATE ".")
target_link_libraries(sampler-test PRIVATE touchseq-rubberband)
target_compile_options(sampler-test PRIVATE -UNDEBUG)  # its checks are asserts
