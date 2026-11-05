cmake_minimum_required(VERSION 3.15)
project(touchseq-acid)

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

# Open303 (MIT), the engine jc303 uses: the exact sources it compiles. math_approx is only needed by the
# diode filter that Open303 includes; the tiny header here is enough for it (we use the TeeBee filter).
set(OPEN303_DIR "${CMAKE_SOURCE_DIR}/open303")
set(OPEN303_SOURCES
  ${OPEN303_DIR}/GlobalFunctions.cpp
  ${OPEN303_DIR}/rosic_AcidPattern.cpp
  ${OPEN303_DIR}/rosic_AcidSequencer.cpp
  ${OPEN303_DIR}/rosic_AnalogEnvelope.cpp
  ${OPEN303_DIR}/rosic_BiquadFilter.cpp
  ${OPEN303_DIR}/rosic_BlendOscillator.cpp
  ${OPEN303_DIR}/rosic_Complex.cpp
  ${OPEN303_DIR}/rosic_DecayEnvelope.cpp
  ${OPEN303_DIR}/rosic_EllipticQuarterBandFilter.cpp
  ${OPEN303_DIR}/rosic_FourierTransformerRadix2.cpp
  ${OPEN303_DIR}/rosic_FunctionTemplates.cpp
  ${OPEN303_DIR}/rosic_LeakyIntegrator.cpp
  ${OPEN303_DIR}/rosic_MidiNoteEvent.cpp
  ${OPEN303_DIR}/rosic_MipMappedWaveTable.cpp
  ${OPEN303_DIR}/rosic_NumberManipulations.cpp
  ${OPEN303_DIR}/rosic_OnePoleFilter.cpp
  ${OPEN303_DIR}/rosic_Open303.cpp
  ${OPEN303_DIR}/rosic_RealFunctions.cpp
  ${OPEN303_DIR}/rosic_TeeBeeFilter.cpp
  ${OPEN303_DIR}/dfl_DiodeLadderFilter.cpp
  ${OPEN303_DIR}/dfl_LFO.cpp)

# RTNeural (BSD-3), for the GuitarML BYOD overdrive model. The STL backend keeps it dependency-free.
set(RTNEURAL_DIR "${CMAKE_SOURCE_DIR}/rtneural")
set(RTNEURAL_DEFS RTNEURAL_USE_EIGEN=0 RTNEURAL_USE_XSIMD=0 RTNEURAL_DEFAULT_ALIGNMENT=16)

dpf_add_plugin(touchseq-acid
  TARGETS lv2 clap
  FILES_DSP AcidPlugin.cpp)
target_sources(touchseq-acid PRIVATE ${OPEN303_SOURCES})
target_include_directories(touchseq-acid PUBLIC "." "${OPEN303_DIR}" "${RTNEURAL_DIR}"
                           "${RTNEURAL_DIR}/modules/json")
target_compile_definitions(touchseq-acid PRIVATE ${RTNEURAL_DEFS})

# DSP test: ./acid-test
add_executable(acid-test test.cpp ${OPEN303_SOURCES})
target_include_directories(acid-test PRIVATE "${OPEN303_DIR}" "${RTNEURAL_DIR}"
                            "${RTNEURAL_DIR}/modules/json")
target_compile_definitions(acid-test PRIVATE ${RTNEURAL_DEFS})
target_compile_options(acid-test PRIVATE -UNDEBUG)  # its checks are asserts
