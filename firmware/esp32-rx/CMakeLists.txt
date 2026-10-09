cmake_minimum_required(VERSION 3.16)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)

if(NOT DEFINED PROJECT_VER)
    set(PROJECT_VER "0.0.0")
endif()

project(wifi_sensing_rx)
