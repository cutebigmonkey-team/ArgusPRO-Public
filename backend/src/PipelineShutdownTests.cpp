#include "PipelineDb.h"

#include <chrono>
#include <csignal>
#include <iostream>
#include <thread>

int main()
{
    using namespace std::chrono_literals;
    argus::pipeline::installSignalHandlers();
    const auto started = std::chrono::steady_clock::now();
    std::thread waiting([] { argus::pipeline::interruptibleWait(30); });
    std::this_thread::sleep_for(20ms);
    std::raise(SIGTERM);
    waiting.join();
    const auto elapsed = std::chrono::steady_clock::now() - started;
    if (!argus::pipeline::stopping() || elapsed >= 1s)
    {
        std::cerr << "FAIL: SIGTERM did not interrupt bounded worker wait\n";
        return 1;
    }
    std::cout << "pipeline graceful shutdown test passed\n";
    return 0;
}
