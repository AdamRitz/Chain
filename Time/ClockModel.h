#ifndef CHAIN_CLOCK_MODEL_H
#define CHAIN_CLOCK_MODEL_H
#include <array>
#include <cmath>
#include <fstream>
#include <string>
#include <vector>
#include <nlohmann/json.hpp>
#include <sodium.h>

// Shared by the C++ extractor and Test/Clock/Train.py. Units are explicit in the names.
inline const std::array<std::string,32> clockFeatureNames={
    "sources","offset_abs_ms","offset_mad_ms","offset_range_ms","rtt_p50_ms","rtt_p95_ms",
    "jitter_ms","age_max_ms","offset_change_ms","rejected_ratio","cpu_cores",
    "pending_tx","pending_block","ready_txs","prepare_p95_ms","commit_p95_ms",
    "mad_1s_ms","rtt_1s_ms","prepare_1s_ms","commit_1s_ms","late_1s_fraction",
    "mad_5s_ms","rtt_5s_ms","prepare_5s_ms","commit_5s_ms","late_5s_fraction",
    "mad_30s_ms","rtt_30s_ms","prepare_30s_ms","commit_30s_ms","late_30s_fraction","sample_rate_hz"};
using ClockFeatures=std::array<double,32>;
struct ClockPrediction {
    double timeUs=0,prepareUs=0; bool valid=false; std::string reason="model_missing";
    bool timeValid=true,prepareValid=true;
    bool HasTime() const { return valid&&timeValid; }
    bool HasPrepare() const { return valid&&prepareValid; }
};
struct ClockQuantile { double bias=0,calibration=0; ClockFeatures weights{}; };
struct ClockPrepareTreeNode { int feature=-1,left=-1,right=-1; double threshold=0,value=0; };
struct ClockPrepareModel {
    bool loaded=false;
    double quantile=.75,bias=0,rate=0,calibration=0;
    ClockFeatures minimum{},maximum{};
    std::vector<size_t> features;
    std::vector<std::vector<ClockPrepareTreeNode>> trees;
};
struct ClockModel {
    bool loaded=false;
    std::string id,sha256,error="model_missing",trainingKind;
    ClockFeatures mean{},scale{},minimum{},maximum{};
    ClockQuantile time,prepare;
    ClockPrepareModel preparation;

    bool Load(const std::string& path) {
        loaded=false; id.clear(); sha256.clear(); trainingKind.clear(); preparation={};
        try {
            std::ifstream input(path,std::ios::binary);
            if (!input) throw std::runtime_error("model_open_failed");
            std::string bytes(131073,'\0'); input.read(bytes.data(),bytes.size()); bytes.resize(size_t(input.gcount()));
            if (bytes.size()>131072) throw std::runtime_error("model_too_large");
            auto json=nlohmann::json::parse(bytes);
            if (json.at("schema")!=1||json.at("feature_schema")!="chain-clock-v1-32"||
                json.at("features").get<std::array<std::string,32>>()!=clockFeatureNames||
                json.at("quantile").get<double>()!=0.95) throw std::runtime_error("model_schema_mismatch");
            id=json.at("model_id").get<std::string>();
            if (id.empty()||id.size()>96) throw std::runtime_error("model_id_invalid");
            trainingKind=json.at("training").at("kind").get<std::string>();
            if (trainingKind!="simulation"&&trainingKind!="controlled-reference") throw std::runtime_error("model_label_source_invalid");
            if (json.at("training").at("split")!="run") throw std::runtime_error("model_split_invalid");
            auto dataset=json.at("training").at("dataset_sha256").get<std::string>();
            if (dataset.size()!=64||dataset.find_first_not_of("0123456789abcdef")!=std::string::npos) throw std::runtime_error("model_dataset_hash_invalid");
            mean=json.at("mean").get<ClockFeatures>(); scale=json.at("scale").get<ClockFeatures>();
            minimum=json.at("minimum").get<ClockFeatures>(); maximum=json.at("maximum").get<ClockFeatures>();
            for (size_t i=0;i<32;i++) {
                if (!std::isfinite(mean[i])||!std::isfinite(scale[i])||scale[i]<=0||!std::isfinite(minimum[i])||
                    !std::isfinite(maximum[i])||minimum[i]>maximum[i]) throw std::runtime_error("model_domain_invalid");
            }
            auto Read=[&](const char* name,ClockQuantile& target) {
                const auto& value=json.at(name); target.bias=value.at("bias_ms").get<double>();
                target.calibration=value.at("calibration_ms").get<double>(); target.weights=value.at("weights").get<ClockFeatures>();
                if (!std::isfinite(target.bias)||std::abs(target.bias)>10000||!std::isfinite(target.calibration)||
                    target.calibration<0||target.calibration>10000) throw std::runtime_error("model_output_invalid");
                for (auto weight:target.weights) if (!std::isfinite(weight)||std::abs(weight)>10000) throw std::runtime_error("model_weight_invalid");
            };
            Read("time",time); Read("prepare",prepare);
            if (json.contains("preparation")) {
                const auto& p=json.at("preparation");
                if (p.at("kind")!="gradient-boosted-quantile"||p.at("label_source")!="local-monotonic"||
                    p.at("split")!="whole-experiment"||p.at("dataset_sha256").get<std::string>().size()!=64)
                    throw std::runtime_error("prepare_provenance_invalid");
                preparation.quantile=p.at("quantile"); preparation.bias=p.at("bias_ms");
                preparation.rate=p.at("learning_rate"); preparation.calibration=p.at("calibration_ms");
                preparation.minimum=p.at("minimum").get<ClockFeatures>(); preparation.maximum=p.at("maximum").get<ClockFeatures>();
                preparation.features=p.at("features").get<std::vector<size_t>>();
                if (preparation.features.empty()||preparation.features.size()>32||!std::isfinite(preparation.quantile)||
                    preparation.quantile<.5||preparation.quantile>.99||!std::isfinite(preparation.bias)||
                    std::abs(preparation.bias)>1000||!std::isfinite(preparation.rate)||preparation.rate<=0||preparation.rate>1||
                    !std::isfinite(preparation.calibration)||preparation.calibration<0||preparation.calibration>1000)
                    throw std::runtime_error("prepare_model_invalid");
                std::array<bool,32> used{};
                for (auto feature:preparation.features) {
                    if (feature>=32||used[feature]||!std::isfinite(preparation.minimum[feature])||
                        !std::isfinite(preparation.maximum[feature])||preparation.minimum[feature]>preparation.maximum[feature])
                        throw std::runtime_error("prepare_domain_invalid");
                    used[feature]=true;
                }
                const auto& trees=p.at("trees");
                if (!trees.is_array()||trees.empty()||trees.size()>64) throw std::runtime_error("prepare_trees_invalid");
                for (const auto& tree:trees) {
                    if (!tree.is_array()||tree.empty()||tree.size()>31) throw std::runtime_error("prepare_tree_invalid");
                    std::vector<ClockPrepareTreeNode> nodes;
                    for (size_t i=0;i<tree.size();i++) {
                        const auto& row=tree.at(i); ClockPrepareTreeNode node;
                        node.feature=row.at("feature"); node.left=row.at("left"); node.right=row.at("right");
                        node.threshold=row.at("threshold"); node.value=row.at("value");
                        if (!std::isfinite(node.threshold)||!std::isfinite(node.value)||std::abs(node.value)>1000||
                            (node.feature==-1?(node.left!=-1||node.right!=-1):
                             (node.feature<0||node.feature>=32||!used[size_t(node.feature)]||node.left<=int(i)||
                              node.right<=int(i)||node.left>=int(tree.size())||node.right>=int(tree.size()))))
                            throw std::runtime_error("prepare_node_invalid");
                        nodes.push_back(node);
                    }
                    preparation.trees.push_back(std::move(nodes));
                }
                preparation.loaded=true;
            }
            std::array<unsigned char,crypto_hash_sha256_BYTES> hash{};
            crypto_hash_sha256(hash.data(),reinterpret_cast<const unsigned char*>(bytes.data()),bytes.size());
            char encoded[crypto_hash_sha256_BYTES*2+1]; sodium_bin2hex(encoded,sizeof(encoded),hash.data(),hash.size()); sha256=encoded;
            loaded=true; error="ok";
        } catch (const std::exception& e) { error=std::string(e.what()).substr(0,256); }
        return loaded;
    }
    ClockPrediction Predict(const ClockFeatures& features) const {
        if (!loaded) return {0,0,false,error};
        ClockFeatures normalized{}; bool timeValid=true;
        for (size_t i=0;i<32;i++) {
            if (!std::isfinite(features[i])) return {0,0,false,"nonfinite_feature"};
            if (features[i]<minimum[i]||features[i]>maximum[i]) timeValid=false;
            normalized[i]=(features[i]-mean[i])/scale[i];
        }
        auto Evaluate=[&](const ClockQuantile& head) {
            double value=head.bias+head.calibration;
            for (size_t i=0;i<32;i++) value+=head.weights[i]*normalized[i];
            return value*1000;
        };
        double uncertainty=timeValid?Evaluate(time):0,delay=timeValid?Evaluate(prepare):0;
        timeValid=timeValid&&std::isfinite(uncertainty)&&uncertainty<=1000000;
        bool prepareValid=timeValid&&std::isfinite(delay)&&delay<=1000000;
        if (preparation.loaded) {
            prepareValid=true;
            for (auto index:preparation.features)
                if (features[index]<preparation.minimum[index]||features[index]>preparation.maximum[index]) prepareValid=false;
            if (prepareValid) {
                delay=preparation.bias+preparation.calibration;
                for (const auto& tree:preparation.trees) {
                    size_t index=0;
                    while (tree[index].feature>=0) {
                        const auto& node=tree[index]; index=size_t(features[size_t(node.feature)]<=node.threshold?node.left:node.right);
                    }
                    delay+=preparation.rate*tree[index].value;
                }
                delay*=1000; prepareValid=std::isfinite(delay)&&delay<=1000000;
            }
        }
        bool valid=timeValid||prepareValid;
        return {timeValid?std::max(250.0,uncertainty):0,prepareValid?std::max(preparation.loaded?1000.0:250.0,delay):0,valid,
                valid?(timeValid?"ok":"time_fallback_prepare_active"):"feature_out_of_domain",timeValid,prepareValid};
    }
};
#endif
