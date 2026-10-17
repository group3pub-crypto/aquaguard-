# Host-side build + tests for the portable firmware core (no Arduino/ESP32 needed).
CXX ?= g++
CXXFLAGS ?= -std=c++17 -O1 -g -Wall -Wextra -Wpedantic -Werror -fsanitize=address,undefined
SRC := aquaguard_sensor/aquaguard_core.cpp
BUILD := build

.PHONY: test payloads clean
test: $(BUILD)/test_core
	$(BUILD)/test_core

payloads: $(BUILD)/emit_payloads
	$(BUILD)/emit_payloads

$(BUILD)/test_core: test/test_core.cpp $(SRC) aquaguard_sensor/aquaguard_core.h | $(BUILD)
	$(CXX) $(CXXFLAGS) -Iaquaguard_sensor $< $(SRC) -o $@

$(BUILD)/emit_payloads: test/emit_payloads.cpp $(SRC) aquaguard_sensor/aquaguard_core.h | $(BUILD)
	$(CXX) $(CXXFLAGS) -Iaquaguard_sensor $< $(SRC) -o $@

$(BUILD):
	mkdir -p $(BUILD)

clean:
	rm -rf $(BUILD)
