#include <filesystem>
#include <fstream>
#include <iostream>
#include "../Network/Server.h"
using namespace std;
void Require(bool value,const char* label) { if (!value) throw runtime_error(label); }
void TestServoAndRounds() {
    ClockServo servo; servo.Init(1000,1000000,-5000,10000);
    int64_t last=1000000;
    for (int i=1;i<20000;i++) {
        auto at=1000+int64_t(i)*100;
        servo.SetRate(at,i%2?1e9:-1e9,at+500);
        auto value=servo.Advance(at+50);
        Require(value>=last&&servo.Rate()>0&&abs(servo.slewPpm)<=10000,"continuous positive bounded clock under alternating adversarial commands");
        last=value;
    }
    servo.SetRate(3000000,-1000,3001000);
    auto before=servo.Advance(3000000); auto after=servo.Advance(3005000);
    Require(servo.slewPpm==0&&after-before>=4970&&after-before<=4975,"expiry splits old rate and holdover even when observer stalls");
    ClockMapping mapping{5000,1005000,1,7};
    auto current=PlanCandidateRound(mapping,19,50000,12000,2000,1000,500,true);
    Require(current&&current->id==20&&current->joinedCurrent,"join a still feasible current round");
    auto next=PlanCandidateRound(mapping,20,50000,12000,2000,1000,500,true);
    Require(next&&next->id==21&&!next->joinedCurrent,"one attempt per round including cancellation");
    auto expensive=PlanCandidateRound(mapping,19,50000,12000,20000,1000,500,true);
    Require(expensive&&expensive->id==21,"expensive preparation can start ahead of a future round");
    auto closed=PlanCandidateRound(mapping,19,50000,12000,1000,12000,500,true);
    Require(!closed,"uncertainty and guard may suppress a proposal");
    auto legacy=PlanCandidateRound(mapping,19,50000,12000,2000,1000,500,false);
    Require(legacy&&legacy->id==21,"whole future round comparator");
    auto frozen=*current;
    mapping.rate=1.01; mapping.version++;
    Require(frozen.clockVersion==7&&frozen.proposalDeadlineUs==10500&&frozen.collectionDeadlineUs==12000,"later model or rate changes cannot move frozen deadlines");
    Require(!CandidateRoundFits(frozen,11000,0,0,{}),"strict local deadline");
}
void TestModel(const filesystem::path& path,const filesystem::path& root) {
    ClockModel model; Require(model.Load(path.string()),"trained model loads");
    auto features=model.mean; auto prediction=model.Predict(features);
    Require(prediction.valid&&prediction.timeUs>=250&&prediction.prepareUs>=250&&model.sha256.size()==64,"finite quantiles and model provenance");
    double time=model.time.bias+model.time.calibration,prepare=model.prepare.bias+model.prepare.calibration;
    Require(abs(prediction.timeUs-max(250.0,time*1000))<1e-5&&abs(prediction.prepareUs-max(250.0,prepare*1000))<1e-5,"normalization and millisecond to microsecond parity");
    features[3]=model.maximum[3]+1;
    Require(!model.Predict(features).valid,"out of domain observations fall back");
    features=model.mean; features[4]=numeric_limits<double>::infinity();
    Require(!model.Predict(features).valid,"nonfinite features fall back");
    ifstream input(path); auto good=nlohmann::json::parse(input);
    for (const auto& kind:{"schema","scale","weight","label","split"}) {
        auto broken=good;
        if (kind==string("schema")) broken["schema"]=99;
        else if (kind==string("scale")) broken["scale"][0]=0;
        else if (kind==string("weight")) broken["time"]["weights"][0]=1e100;
        else if (kind==string("label")) broken["training"]["kind"]="neighbor_median";
        else broken["training"]["split"]="random-row";
        auto file=root/(string(kind)+".json"); { ofstream output(file); output<<broken; }
        Require(!model.Load(file.string())&&!model.Predict(model.mean).valid,"invalid model cannot retain previously active predictions");
    }
    Require(!model.Load((root/"missing.json").string()),"missing file is a baseline fallback");
}
void TestSourceAndFallback() {
    nodeClock.control="learned"; nodeClock.enabled=true; nodeClock.members={1,2,3}; nodeClock.minPeers=3;
    InitTime();
    auto Sample=[](uint64_t peer,int64_t offset) {
        auto t1=GetLogicalTimeUs(); return RecordClockSample(peer,t1,t1+offset+100,t1+offset+120,t1+220,220);
    };
    Require(!Sample(4,1000),"unconfigured endpoints do not gain time weight");
    Require(Sample(1,1000)&&Sample(1,1500)&&nodeClock.sources.size()==1,"repeated sessions have one contribution per stable endpoint");
    AdjustClock(); Require(nodeClock.fallback=="insufficient_fresh_sources"&&nodeClock.servo.slewPpm==0,"fresh source quorum gate");
    Require(GetCandidateBudgets().first>=1000000,"incomplete source quorum suppresses local proposals independently of observer");
    Require(Sample(2,900)&&Sample(3,1000000),"collect honest sources and one false report");
    nodeClock.prediction={1000,1000,true,"ok"}; nodeClock.predictionAt=int64_t(GetSteadyTime()/1000)-3000000;
    AdjustClock(); Require(nodeClock.fallback=="prediction_stale"&&abs(nodeClock.servo.slewPpm)<=1000,"stale model falls back to bounded robust control");
    Require(nodeClock.servo.slewPpm<900,"one large false report cannot dominate the median target");
    Require(!RecordClockSample(1,INT64_MIN,INT64_MAX,INT64_MAX,INT64_MAX,1),"malicious timestamps do not overflow signed subtraction");
    for (auto& [key,source]:nodeClock.sources) source.at-=2000000;
    AdjustClock(); Require(nodeClock.servo.slewPpm==0&&nodeClock.fallback=="insufficient_fresh_sources","expired sources force holdover");
    Require(GetCandidateBudgets().first>=1000000,"expired sources cannot leave a stale narrow scheduling budget");
}
void TestPreparedState(const filesystem::path& root) {
    auto owner=GenerateWallet(),receiver=GenerateWallet();
    SetGenesisUsers({{"base_fee",1},{"accounts",nlohmann::json::array({{{"public_key",U32ToHex(owner.public_key)},{"balance",1000}}})}});
    InitDB((root/"db").string()); GenerateGenesisBlock();
    auto tx=GenerateTx(owner.public_key,receiver.public_key,1,1,owner);
    Require(ProcessTxPackage(tx)==1,"prepare funded transaction");
    auto data=GenerateBlock(); Block block;
    Require(VerifyBlock(data,&block)&&DBCheckBlockUsers(data),"prepare and validate against parent state before publishing");
    auto head=ReadChainHead(); auto now=int64_t(GetSteadyTime()/1000);
    CandidateRound round; round.startUs=now-1000; round.proposalDeadlineUs=now+100000; round.height=head.height+1;
    round.parent=head.hash; round.stateVersion=head.version+1;
    Require(!SelectBlockCandidate(block,data,&round)&&!blockReady,"state version mismatch discards prepared work");
    round.stateVersion=head.version; round.proposalDeadlineUs=now-1;
    Require(!SelectBlockCandidate(block,data,&round)&&!blockReady,"late local work never enters the candidate pool");
    // Deadline is a local scheduling policy, never a received-block validity rule.
    Require(ProcessBlock(data)&&blockReady,"equally late valid received block follows the ordinary consensus path");
    Require(CommitBlock(block,data),"received candidate commits atomically");
    round.proposalDeadlineUs=int64_t(GetSteadyTime()/1000)+100000;
    Require(!SelectBlockCandidate(block,data,&round),"parent change invalidates old prepared execution");
    Require(GetUser(users,owner.public_key).nonce==1&&GetUser(users,receiver.public_key).balance==1,"published chain and account state agree");
    db.reset();
}
void TestPreparationModel(const filesystem::path& base,const filesystem::path& root) {
    auto path=base.parent_path()/"preparation-v1.json";
    ClockModel model; Require(model.Load(path.string())&&model.preparation.loaded,"real work model loads independently of time reference");
    ifstream input(path); auto pack=nlohmann::json::parse(input);
    for (const auto& row:pack["preparation"]["parity_cases"]) {
        auto prediction=model.Predict(row["features"].get<ClockFeatures>());
        Require(prediction.HasPrepare()&&abs(prediction.prepareUs-row["prepare_us"].get<double>())<1e-5,"exported tree inference agrees with offline training");
    }
    auto features=pack["preparation"]["parity_cases"].back()["features"].get<ClockFeatures>();
    features[3]=model.maximum[3]+1;
    auto prediction=model.Predict(features);
    Require(prediction.HasPrepare()&&!prediction.HasTime(),"time domain failure does not disable locally labelled work prediction");
    features[14]=model.preparation.maximum[14]+1;
    Require(!model.Predict(features).HasPrepare(),"unseen work domain keeps preparation fallback");
    auto broken=pack; broken["preparation"]["trees"][0][0]["left"]=0;
    auto invalid=root/"cyclic-tree.json"; { ofstream out(invalid); out<<broken; }
    Require(!model.Load(invalid.string()),"cyclic trees cannot hang inference");
    nodeClock.model.Load(path.string()); nodeClock.control="learned";
    nodeClock.prediction=prediction; nodeClock.predictionAt=int64_t(GetSteadyTime()/1000);
    nodeClock.preparations.clear(); nodeClock.prepareQuantile=.75;
    for (int i=0;i<10;i++) RecordClockWork(true,100000);
    auto budget=GetCandidateBudgets();
    Require(budget.second==int64_t(ceil(prediction.prepareUs))&&budget.first>=1000000,"soft model lead may reduce historical work budget without bypassing source quorum");
    nodeClock.predictionAt-=3000000;
    Require(GetCandidateBudgets().second==100000,"expired preparation prediction returns to the same rule quantile");
    nodeClock.prepareQuantile=.95;
}
int main(int argc,char** argv) {
    try {
        if (argc!=3||sodium_init()<0) return 1;
        auto root=filesystem::path(argv[2])/to_string(GetSteadyTime()); filesystem::create_directories(root);
        TestServoAndRounds(); TestModel(argv[1],root); TestSourceAndFallback(); TestPreparationModel(argv[1],root); TestPreparedState(root);
        cout<<"{\"checks\":[\"continuous slew and expiry\",\"frozen rounds\",\"model schema and domain\",\"source quorum\",\"local deadline vs received validity\",\"parent state binding\"]}"<<endl;
        return 0;
    } catch (const exception& error) { cerr<<error.what()<<endl; return 1; }
}
