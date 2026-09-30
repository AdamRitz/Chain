import java.math.BigInteger;
import java.nio.file.*;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.*;
import java.util.concurrent.locks.LockSupport;
import com.google.gson.Gson;
import com.google.gson.GsonBuilder;
import com.google.gson.JsonParser;
import org.fisco.bcos.sdk.jni.utilities.tx.TransactionBuilderJniObj;
import org.fisco.bcos.sdk.v3.BcosSDK;
import org.fisco.bcos.sdk.v3.client.Client;
import org.fisco.bcos.sdk.v3.model.TransactionReceipt;
import org.fisco.bcos.sdk.v3.model.callback.TransactionCallback;
import org.fisco.bcos.sdk.demo.contract.ParallelOk;
import org.fisco.bcos.sdk.demo.contract.DagTransfer;

/** Successful committed transfers, prepared signatures, monotonic timing. */
public class FiscoBench {
    static final BigInteger INITIAL = BigInteger.valueOf(1_000_000_000L);
    static int USERS = 1024;
    static final Gson JSON = new GsonBuilder().setPrettyPrinting().create();
    static BcosSDK sdk;
    static Client client;
    static ParallelOk contract;
    static DagTransfer nativeContract;
    static boolean nativeMode;
    static String prefix;
    static int fundingReceiptQueries;
    static long Number(String value) {
        return value.startsWith("0x") ? Long.parseLong(value.substring(2),16) : Long.parseLong(value);
    }
    static String Name(int user) { return prefix + "-" + user; }
    static String Hash(String signed) {
        try {return JsonParser.parseString(TransactionBuilderJniObj.decodeTransactionToJsonObj(signed))
            .getAsJsonObject().get("dataHash").getAsString();}
        catch(Exception e){throw new IllegalStateException(e);}
    }
    static boolean Success(TransactionReceipt receipt) {
        return receipt != null && receipt.getStatus() == 0 && (!nativeMode ||
            "0x0000000000000000000000000000000000000000000000000000000000000000".equals(receipt.getOutput()));
    }
    static Map<String,Object> Snapshot() {
        var count = client.getTotalTransactionCount().getTotalTransactionCount();
        var row = new LinkedHashMap<String,Object>();
        row.put("epoch_ms",System.currentTimeMillis());
        row.put("count",Number(count.getTransactionCount()));
        row.put("failed",Number(count.getFailedTransactionCount()));
        row.put("height",Number(count.getBlockNumber()));
        row.put("pending",client.getPendingTxSize().getPendingTxSize());
        return row;
    }
    static void Fund() throws Exception {
        CountDownLatch latch = new CountDownLatch(USERS);
        AtomicInteger failed = new AtomicInteger();
        for (int i=0;i<USERS;i++) {
            TransactionCallback cb = new TransactionCallback() {
                public void onResponse(TransactionReceipt r) {
                    if (r.getStatus()!=0 || (nativeMode && nativeContract.getUserAddOutput(r).getValue1().signum()!=0)) failed.incrementAndGet();
                    latch.countDown();
                }
                public void onError(int code,String msg) { failed.incrementAndGet(); latch.countDown(); }
            };
            if (nativeMode) nativeContract.userAdd(Name(i),INITIAL,cb); else contract.set(Name(i),INITIAL,cb);
        }
        if (!latch.await(10,TimeUnit.SECONDS) || failed.get()!=0) {
            // Setup is outside the measured interval. Verify state when a push receipt is delayed.
            fundingReceiptQueries=(int)latch.getCount()+failed.get();
            AtomicInteger wrong=new AtomicInteger();ExecutorService verify=Executors.newFixedThreadPool(8);
            for(int i=0;i<USERS;i++){int index=i;verify.submit(()->{
                try {
                    var balance=nativeMode?nativeContract.userBalance(Name(index)).getValue2():contract.balanceOf(Name(index));
                    if(!balance.equals(INITIAL))wrong.incrementAndGet();
                }catch(Exception e){wrong.incrementAndGet();}
            });}
            verify.shutdown();if(!verify.awaitTermination(120,TimeUnit.SECONDS)||wrong.get()!=0)
                throw new IllegalStateException("Funding state mismatch: "+wrong);
        }
    }
    static double Percentile(long[] data,double q) {
        return data.length==0 ? 0 : data[(int)Math.min(data.length-1,Math.ceil(q*data.length)-1)]/1e6;
    }
    public static void main(String[] args) throws Exception {
        // config mode rate warmupSeconds measureSeconds outputJSON
        Path output = Paths.get(args[5]); Files.createDirectories(output.toAbsolutePath().getParent());
        nativeMode = args[1].equals("native");
        int rate = Integer.parseInt(args[2]), warm = Integer.parseInt(args[3]), seconds = Integer.parseInt(args[4]);
        if(args.length>6)USERS=Integer.parseInt(args[6]);
        if(USERS<=0 || USERS%2!=0)throw new IllegalArgumentException("Use a positive even account count");
        int maxInflight=args.length>8?Integer.parseInt(args[8]):100000;
        int total = Math.multiplyExact(rate,warm+seconds);
        prefix = "bench"+System.currentTimeMillis()+"-"+UUID.randomUUID().toString().substring(0,8);
        var result = new LinkedHashMap<String,Object>();
        result.put("workload",args[1]); result.put("target_tps",rate); result.put("warmup_s",warm);
        result.put("measure_s",seconds); result.put("users",USERS); result.put("requested",total);
        result.put("prefix",prefix); result.put("presigned",true);
        result.put("max_inflight",maxInflight);
        sdk = BcosSDK.build(args[0]); client = sdk.getClient("group0");
        try {
            var key = client.getCryptoSuite().getCryptoKeyPair();
            if (nativeMode) {
                nativeContract=DagTransfer.load("0x000000000000000000000000000000000000100c",client,key);
                nativeContract.setEnableDAG(true);
                result.put("contract",nativeContract.getContractAddress());
                // The v1 executor's fresh genesis may omit the demo's test table.
                var status=nativeContract.userBalance("table-check").getValue1();
                if(status.equals(BigInteger.ONE.shiftLeft(256).subtract(BigInteger.valueOf(51406)))) {
                    var code=new org.fisco.bcos.sdk.v3.contract.precompiled.crud.KVTableService(client,key)
                        .createTable("dag_transfer","user_name","value");
                    if(code.getCode()!=0)throw new IllegalStateException("Create test table: "+code);
                    result.put("created_native_table",true);
                }
            } else {
                contract=ParallelOk.deploy(client,key,true); contract.setEnableDAG(true);
                result.put("contract",contract.getContractAddress());
            }
            Fund();
            result.put("funding_delayed_receipts",fundingReceiptQueries);
            System.out.println("PREPARE "+total+" "+args[1]); System.out.flush();
            long prepareStart=System.nanoTime();
            String[] signed = new String[total];
            String[] hashes = new String[total];
            // Alternating directions over disjoint account pairs; only funded accounts transact.
            java.util.stream.IntStream.range(0,total).parallel().forEach(i -> {
                int from=(i%USERS), to=from^1;
                signed[i]=nativeMode ? nativeContract.getSignedTransactionForUserTransfer(Name(from),Name(to),BigInteger.ONE)
                    : contract.getSignedTransactionForTransfer(Name(from),Name(to),BigInteger.ONE);
                hashes[i]=Hash(signed[i]);
            });
            result.put("prepare_s",(System.nanoTime()-prepareStart)/1e9);
            long[] sent=new long[total], completed=new long[total]; byte[] outcomes=new byte[total];
            AtomicInteger next=new AtomicInteger(), submitted=new AtomicInteger(), success=new AtomicInteger(), failure=new AtomicInteger();
            CountDownLatch received=new CountDownLatch(total);
            AtomicLong lastCompletion=new AtomicLong();
            AtomicReferenceArray<TransactionCallback> callbacks=new AtomicReferenceArray<>(total);
            ConcurrentHashMap<String,AtomicInteger> errors=new ConcurrentHashMap<>();
            List<Map<String,Object>> samples=Collections.synchronizedList(new ArrayList<>());
            var initial=Snapshot(); result.put("before",initial);
            if(args.length>7) {
                Path gate=Paths.get(args[7]);Files.writeString(Paths.get(args[7]+".ready"),prefix);
                long waitUntil=System.currentTimeMillis()+180000;
                while(!Files.exists(gate)&&System.currentTimeMillis()<waitUntil)Thread.sleep(25);
                if(!Files.exists(gate))throw new IllegalStateException("Start gate timed out");
                long target=Long.parseLong(Files.readString(gate).trim());
                while(System.currentTimeMillis()<target)LockSupport.parkNanos(500000);
            }
            long start=System.nanoTime(); long epoch=System.currentTimeMillis();
            result.put("start_epoch_ms",epoch);
            System.out.println("START "+epoch);System.out.flush();
            ScheduledExecutorService monitor=Executors.newSingleThreadScheduledExecutor();
            monitor.scheduleAtFixedRate(()->{
                try {
                    var row=Snapshot();row.put("elapsed_s",(System.nanoTime()-start)/1e9);
                    row.put("submitted",submitted.get());row.put("success",success.get());row.put("failed_callbacks",failure.get());samples.add(row);
                    if(samples.size()%10==0){System.out.println("PROGRESS submitted="+submitted+" success="+success+" failed="+failure);System.out.flush();}
                } catch(Exception e) { var row=new LinkedHashMap<String,Object>();row.put("error",e.toString());samples.add(row); }
            },0,1,TimeUnit.SECONDS);
            ExecutorService senders=Executors.newFixedThreadPool(4);
            for(int worker=0;worker<4;worker++) senders.submit(()->{
                for(int i;(i=next.getAndIncrement())<total;) {
                    int index=i; long due=start+(long)(i*1e9/rate);
                    long delay;while((delay=due-System.nanoTime())>0) LockSupport.parkNanos(Math.min(delay,1_000_000));
                    while(submitted.get()-success.get()-failure.get()>maxInflight) LockSupport.parkNanos(1_000_000);
                    sent[index]=System.nanoTime();submitted.incrementAndGet();
                    TransactionCallback callback=new TransactionCallback(){
                        final AtomicBoolean done=new AtomicBoolean();
                        void Complete(boolean ok,String error){
                            if(!done.compareAndSet(false,true))return;
                            long now=System.nanoTime();completed[index]=now;outcomes[index]=(byte)(ok?1:2);
                            lastCompletion.accumulateAndGet(now,Math::max);
                            if(ok) success.incrementAndGet();else {failure.incrementAndGet();errors.computeIfAbsent(error,k->new AtomicInteger()).incrementAndGet();}
                            callbacks.set(index,null);
                            received.countDown();
                        }
                        public void onResponse(TransactionReceipt r){
                            try {Complete(Success(r),"receipt:"+r.getStatus()+":"+r.getOutput());}
                            catch(Exception e){Complete(false,e.toString());}
                        }
                        public void onError(int code,String msg){Complete(false,"sdk:"+code+":"+msg);}
                        public void onTimeout(){Complete(false,"timeout");}
                    };
                    callback.setTimeout(120000);
                    callbacks.set(index,callback);
                    try {client.sendTransactionAsync(signed[index],false,callback);}catch(Exception e){callback.onError(-1,e.toString());}
                    signed[index]=null;
                }
            });
            senders.shutdown(); boolean sendFinished=senders.awaitTermination(warm+seconds+180,TimeUnit.SECONDS);
            long sendEnd=System.nanoTime();boolean drainFinished=received.await(10,TimeUnit.SECONDS);
            AtomicInteger recovered=new AtomicInteger();
            AtomicInteger recoveredFailures=new AtomicInteger();
            if(!drainFinished || failure.get()>0) {
                ExecutorService queries=Executors.newFixedThreadPool(8);
                long deadline=System.nanoTime()+60_000_000_000L;
                int queryCursor=0;
                while((received.getCount()>0||failure.get()>0)&&System.nanoTime()<deadline) {
                    List<Future<?>> pendingQueries=new ArrayList<>();
                    for(int scanned=0;scanned<total&&pendingQueries.size()<1000;scanned++) {
                        int i=queryCursor;queryCursor=(queryCursor+1)%total;
                        TransactionCallback cb=callbacks.get(i);if(outcomes[i]==1||sent[i]==0)continue;
                        int index=i;
                        pendingQueries.add(queries.submit(()->{
                            try {
                                var r=client.getTransactionReceipt(hashes[index],false).getTransactionReceipt();
                                if(r!=null&&hashes[index].equalsIgnoreCase(r.getTransactionHash())&&Success(r)){
                                    recovered.incrementAndGet();
                                    if(outcomes[index]==2){
                                        long now=System.nanoTime();completed[index]=now;outcomes[index]=1;
                                        failure.decrementAndGet();success.incrementAndGet();recoveredFailures.incrementAndGet();
                                        lastCompletion.accumulateAndGet(now,Math::max);
                                    }else if(cb!=null)cb.onResponse(r);
                                }
                            }catch(Exception ignored){}
                        }));
                    }
                    for(var f:pendingQueries) {
                        long remaining=deadline-System.nanoTime();
                        if(remaining<=0)break;
                        try{f.get(remaining,TimeUnit.NANOSECONDS);}
                        catch(TimeoutException e){break;}
                    }
                    if(received.getCount()>0||failure.get()>0)Thread.sleep(1000);
                }
                queries.shutdownNow();queries.awaitTermination(5,TimeUnit.SECONDS);
                drainFinished=received.getCount()==0;
            }
            result.put("queried_receipts",recovered.get());
            result.put("recovered_callback_failures",recoveredFailures.get());
            monitor.shutdownNow();monitor.awaitTermination(10,TimeUnit.SECONDS);
            result.put("send_finished",sendFinished);result.put("drain_finished",drainFinished);
            result.put("send_s",(sendEnd-start)/1e9);result.put("drain_s",Math.max(0,(lastCompletion.get()-sendEnd)/1e9));
            result.put("submitted",submitted.get());result.put("success",success.get());result.put("failures",failure.get());
            result.put("unresolved",received.getCount());result.put("errors",errors);result.put("samples",samples);
            long windowStart=start+warm*1_000_000_000L,windowEnd=windowStart+seconds*1_000_000_000L;
            int sentInWindow=0,confirmedInWindow=0,backlogStart=0,backlogEnd=0;long[] lat=new long[total];int n=0;
            long[] expected=new long[USERS];Arrays.fill(expected,INITIAL.longValueExact());
            for(int i=0;i<total;i++) {
                if(sent[i]>=windowStart&&sent[i]<windowEnd){sentInWindow++;if(outcomes[i]==1)lat[n++]=completed[i]-sent[i];}
                if(outcomes[i]==1){expected[i%USERS]--;expected[(i%USERS)^1]++;if(completed[i]>=windowStart&&completed[i]<windowEnd)confirmedInWindow++;}
                if(sent[i]>0&&sent[i]<windowStart&&(completed[i]==0||completed[i]>=windowStart))backlogStart++;
                if(sent[i]>0&&sent[i]<windowEnd&&(completed[i]==0||completed[i]>=windowEnd))backlogEnd++;
            }
            lat=Arrays.copyOf(lat,n);Arrays.sort(lat);
            result.put("actual_send_tps",sentInWindow/(double)seconds);
            result.put("receipt_tps",confirmedInWindow/(double)seconds);
            result.put("overall_tps",success.get()/((lastCompletion.get()-start)/1e9));
            result.put("p50_ms",Percentile(lat,.5));result.put("p95_ms",Percentile(lat,.95));result.put("p99_ms",Percentile(lat,.99));
            long[] scheduledLat=new long[n];int scheduledCount=0;
            for(int i=0;i<total;i++)if(sent[i]>=windowStart&&sent[i]<windowEnd&&outcomes[i]==1)
                scheduledLat[scheduledCount++]=completed[i]-(start+(long)(i*1e9/rate));
            Arrays.sort(scheduledLat);result.put("scheduled_p99_ms",Percentile(scheduledLat,.99));
            Map<Long,Integer> histogram=new TreeMap<>();
            for(long value:lat)histogram.merge((value+999999)/1000000,1,Integer::sum);
            result.put("latency_histogram_ms",histogram);
            result.put("backlog_start",backlogStart);result.put("backlog_end",backlogEnd);
            result.put("backlog_growth_per_s",(backlogEnd-backlogStart)/(double)seconds);
            // Save measured data before post-run verification so failed runs remain inspectable.
            Files.writeString(output,JSON.toJson(result));
            Thread.sleep(1500); result.put("after",Snapshot());
            AtomicInteger mismatches=new AtomicInteger();
            ExecutorService checks=Executors.newFixedThreadPool(8);
            for(int i=0;i<USERS;i++){int index=i;checks.submit(()->{
                try {
                    BigInteger balance;
                    if(nativeMode){var value=nativeContract.userBalance(Name(index));if(value.getValue1().signum()!=0)throw new IllegalStateException("balance status");balance=value.getValue2();}
                    else balance=contract.balanceOf(Name(index));
                    if(!balance.equals(BigInteger.valueOf(expected[index])))mismatches.incrementAndGet();
                }catch(Exception e){mismatches.incrementAndGet();}
            });}
            checks.shutdown();boolean checked=checks.awaitTermination(120,TimeUnit.SECONDS);
            result.put("balance_mismatches",mismatches.get());result.put("balances_checked",checked?USERS:0);
            result.put("expected_balances",expected);
            Files.writeString(output,JSON.toJson(result));
            System.out.println("RESULT "+output+" TPS="+result.get("receipt_tps")+" P99="+result.get("p99_ms")+" failures="+failure+" balances="+mismatches);
        } finally { sdk.stopAll(); }
    }
}


