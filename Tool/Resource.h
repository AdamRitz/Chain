#ifndef CHAIN_RESOURCE_H
#define CHAIN_RESOURCE_H
#include <fstream>
#include <nlohmann/json.hpp>
#ifdef _WIN32
#include <windows.h>
#include <psapi.h>
#else
#include <sys/resource.h>
#include <unistd.h>
#endif

nlohmann::json GetProcessResources() {
    nlohmann::json result;
#ifdef _WIN32
    FILETIME created,exited,kernel,user;
    if (GetProcessTimes(GetCurrentProcess(),&created,&exited,&kernel,&user)) {
        ULARGE_INTEGER k,u;
        k.LowPart=kernel.dwLowDateTime; k.HighPart=kernel.dwHighDateTime;
        u.LowPart=user.dwLowDateTime; u.HighPart=user.dwHighDateTime;
        result["process_cpu_seconds"]=(k.QuadPart+u.QuadPart)/1e7;
    }
    PROCESS_MEMORY_COUNTERS memory{};
    if (GetProcessMemoryInfo(GetCurrentProcess(),&memory,sizeof(memory))) {
        result["resident_bytes"]=memory.WorkingSetSize;
        result["peak_working_set_bytes"]=memory.PeakWorkingSetSize;
    }
    IO_COUNTERS io{};
    if (GetProcessIoCounters(GetCurrentProcess(),&io)) result["process_write_bytes"]=io.WriteTransferCount;
#else
    rusage usage{};
    if (getrusage(RUSAGE_SELF,&usage)==0) {
        result["process_cpu_seconds"]=usage.ru_utime.tv_sec+usage.ru_stime.tv_sec+
            (usage.ru_utime.tv_usec+usage.ru_stime.tv_usec)/1e6;
        result["peak_working_set_bytes"]=uint64_t(usage.ru_maxrss)*1024;
    }
    uint64_t pages=0,resident=0;
    std::ifstream memory("/proc/self/statm");
    if (memory>>pages>>resident) result["resident_bytes"]=resident*uint64_t(sysconf(_SC_PAGESIZE));
    std::ifstream io("/proc/self/io");
    std::string key; uint64_t value;
    while (io>>key>>value) if (key=="write_bytes:") result["process_write_bytes"]=value;
#endif
    return result;
}
#endif
