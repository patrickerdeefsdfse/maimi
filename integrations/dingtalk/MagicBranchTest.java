import java.nio.file.*;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.*;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.util.concurrent.Flow;
import java.nio.ByteBuffer;
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import com.alibaba.fastjson.JSON;
import org.ssssssss.script.*;
public class MagicBranchTest {
  public static class Reply {
    String json; public Reply(String json){this.json=json;}
    public int statusCode(){return 200;} public String body(){return json;}
  }
  public static class Capture {
    int calls;
    public String postForObject(java.net.URI uri,org.springframework.http.HttpEntity entity,Class type) throws Exception {
      calls++;
      if(calls==1) {
        if(!uri.getPath().equals("/internal/maimi-im/v1/dingtalk"))throw new AssertionError();
        byte[] raw=(byte[])entity.getBody();
        var m=JSON.parseObject(new String(raw,StandardCharsets.UTF_8));
        if(!"状态".equals(m.getJSONObject("text").getString("content")) || m.containsKey("sessionWebhook"))throw new AssertionError();
        String at=entity.getHeaders().getFirst("X-Maimi-Time");
        long age=System.currentTimeMillis()/1000-Long.parseLong(at);if(Math.abs(age)>60)throw new AssertionError("Bad time");
        var canonical="POST\n/internal/maimi-im/v1/dingtalk\n"+at+"\n"+entity.getHeaders().getFirst("X-Maimi-Nonce")+"\n"+HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(raw));
        var mac=Mac.getInstance("HmacSHA256");
        mac.init(new SecretKeySpec(Files.readAllBytes(Path.of(System.getenv("MAIMI_IM_FORWARD_KEY_FILE"))),"HmacSHA256"));
        if(!HexFormat.of().formatHex(mac.doFinal(canonical.getBytes(StandardCharsets.UTF_8))).equals(entity.getHeaders().getFirst("X-Maimi-Sign")))throw new AssertionError("signature mismatch");
        return "{\"state\":\"answered\",\"reply\":\"synthetic response\"}";
      }
      if(calls==2 && uri.getHost().equals("oapi.dingtalk.com"))return "{\"errcode\":0}";
      throw new AssertionError();
    }
  }
  public static void main(String[] args) throws Exception {
    String original=Files.readString(Path.of(args[0]));
    String script=Files.readString(Path.of(args[1]));
    String anchor="var wt = lgInputText.trim();";int pos=original.indexOf(anchor);
    if(pos<0 || original.indexOf(anchor,pos+1)>=0)throw new AssertionError("anchor");
    String combined=original.substring(0,pos+anchor.length())+"\n"+script+"\n"+original.substring(pos+anchor.length());
    MagicScript.create(combined,null).compile();
    System.out.println("FULL_PUBLIC_SCRIPT_COMPILED");
    script=script.replace("new MmRest(mmFactory)", "capture");
    String ts=String.valueOf(System.currentTimeMillis());
    String secret=Files.readString(Path.of(System.getenv("MAIMI_IM_DING_SECRET_FILE"))).strip();
    Mac mac=Mac.getInstance("HmacSHA256");mac.init(new SecretKeySpec(secret.getBytes(StandardCharsets.UTF_8),"HmacSHA256"));
    String sign=Base64.getEncoder().encodeToString(mac.doFinal((ts+"\n"+secret).getBytes(StandardCharsets.UTF_8)));
    Map<String,Object> body=new LinkedHashMap<>();
    body.put("conversationType","1");body.put("conversationId","dialog");body.put("robotCode","robot");body.put("msgId","message");body.put("senderCorpId","corp");body.put("chatbotCorpId","corp");body.put("senderStaffId","staff");body.put("createAt",Long.parseLong(ts));body.put("sessionWebhook","https://oapi.dingtalk.com/robot/sendBySession?session=SYNTHETIC");
    var valid=Map.of("timestamp",ts,"sign",sign);var invalid=Map.of("timestamp",ts,"sign","invalid");var capture=new Capture();
    var wrong=Map.of("timestamp",ts,"sign",Base64.getEncoder().encodeToString(new byte[32]));
    var stale=Map.of("timestamp","1000000000000","sign",sign);
    for(var input:List.of(valid,invalid,wrong,stale,Map.<String,String>of())){
      String value=(String)MagicScript.create(script,null).execute(new MagicScriptContext().set("wt","麦米 回调自检").set("capture",capture).set("body",body).set("header",input));
      String expected=input.isEmpty()?"timestamp_present=false;sign_present=false;verification=MISSING_HEADERS":
          "timestamp_present=true;sign_present=true;verification="+(input==valid?"PASSED":input==wrong?"FAILED":input==stale?"STALE_TIMESTAMP":"INVALID_HEADERS");
      if(!expected.equals(value)||capture.calls!=0)throw new AssertionError("unexpected diagnostics");
    }
    for(String text:List.of("物流测试单","麦米饭","姓名20261105")){
      Object result=MagicScript.create(script+"\nreturn 'old';",null).execute(new MagicScriptContext().set("wt",text).set("capture",capture).set("body",body).set("header",valid));
      if(!"old".equals(result)||capture.calls!=0)throw new AssertionError("ungated");
    }
    Object failure=MagicScript.create(script,null).execute(new MagicScriptContext().set("wt","麦米 状态").set("capture",capture).set("body",body).set("header",invalid));
    if(capture.calls!=0 || "OK!".equals(failure))throw new AssertionError("bad signature accepted");
    String instrumented=script.replace("catch (mmError) {","catch (mmError) { throw mmError;");
    Object result=MagicScript.create(instrumented,null).execute(new MagicScriptContext().set("wt","麦米：状态").set("capture",capture).set("body",body).set("header",valid));
    if(!"OK!".equals(result)||capture.calls!=2)throw new AssertionError("expected status+reply: "+result+" "+capture.calls);
    System.out.println("MAGICSCRIPT_ISOLATED_PASS");
  }
}
