//
// Created by 61485 on 2026/5/22.
//

#include "VRF.h"
#include <chrono>
#include <iomanip>
#include <iostream>
#include <vector>

using namespace std;
using namespace std::chrono;

int CrrectnessTest() {
    InitVRFWallet();
    vector<uint8_t> m{1,203,3,4,5,6,7,8,99};
    auto a = VRFGen(m);
    if (VRFVerify(a,m,myVRFWallet.pk)==true) {
        cout<<"VRF Verify Success";
    };
    return 0;
}


int main() {
    if (sodium_init()<0) return 1;
    InitVRFWallet();

    vector<uint8_t> m{1, 203, 3, 4, 5, 6, 7, 8, 99};

    constexpr int warmupRounds = 1000;
    constexpr int testRounds = 100000;

    for (int i = 0; i < warmupRounds; i++) {
        auto proof = VRFGen(m);
        VRFVerify(proof, m, myVRFWallet.pk);
    }

    auto proof = VRFGen(m);

    auto genStart = steady_clock::now();

    for (int i = 0; i < testRounds; i++) {
        proof = VRFGen(m);
    }

    auto genEnd = steady_clock::now();

    int verifySuccess = 0;

    auto verifyStart = steady_clock::now();

    for (int i = 0; i < testRounds; i++) {
        if (VRFVerify(proof, m, myVRFWallet.pk)) {
            verifySuccess++;
        }
    }

    auto verifyEnd = steady_clock::now();

    auto genNs = duration_cast<nanoseconds>(genEnd - genStart).count();
    auto verifyNs = duration_cast<nanoseconds>(verifyEnd - verifyStart).count();

    cout << fixed << setprecision(2);
    cout << "VRFGen total time: " << genNs / 1000000.0 << " ms" << endl;
    cout << "VRFGen average time: " << static_cast<double>(genNs) / testRounds << " ns" << endl;
    cout << "VRFGen TPS: " << testRounds * 1000000000.0 / genNs << endl;

    cout << "VRFVerify total time: " << verifyNs / 1000000.0 << " ms" << endl;
    cout << "VRFVerify average time: " << static_cast<double>(verifyNs) / testRounds << " ns" << endl;
    cout << "VRFVerify TPS: " << testRounds * 1000000000.0 / verifyNs << endl;

    cout << "Verify success count: " << verifySuccess << " / " << testRounds << endl;

    return 0;
}
