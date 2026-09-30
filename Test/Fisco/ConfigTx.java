import org.fisco.bcos.sdk.v3.BcosSDK;
import org.fisco.bcos.sdk.v3.contract.precompiled.sysconfig.SystemConfigService;

/** Submit and verify a dynamic block limit outside the benchmark window. */
public class ConfigTx {
    public static void main(String[] args) throws Exception {
        var sdk=BcosSDK.build(args[0]);
        try {
            var client=sdk.getClient("group0");
            var service=new SystemConfigService(client,client.getCryptoSuite().getCryptoKeyPair());
            var result=service.setValueByKey("tx_count_limit",args[1]);
            if(result.getCode()!=0)throw new IllegalStateException(result.toString());
            System.out.println(client.getSystemConfigByKey("tx_count_limit").getResult());
        } finally {sdk.stopAll();}
    }
}
