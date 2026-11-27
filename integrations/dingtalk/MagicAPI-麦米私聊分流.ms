import 'java.lang.Math' as MmMath;
import 'java.lang.String' as MmString;
import 'java.lang.Long' as MmLong;
import 'java.lang.System' as MmSystem;
// 插入到 var wt = lgInputText.trim(); 后面，原物流逻辑从此段结尾继续。
import java.nio.file.Files;
import java.nio.file.Paths;
import java.nio.charset.StandardCharsets;
import java.net.URI;
import java.util.Base64;
import java.util.UUID;
import java.security.MessageDigest;
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import com.alibaba.fastjson.JSON;
import org.springframework.http.HttpHeaders as MmHeaders;
import org.springframework.http.HttpEntity as MmEntity;
import org.springframework.http.client.SimpleClientHttpRequestFactory as MmFactory;
import org.springframework.web.client.RestTemplate as MmRest;

if (wt.matches("(?s)^麦米(?:$|[\\s\\p{Z}：:].*)")) {
    var mmText = wt.replaceFirst("^麦米[\\s\\p{Z}：:]*", "").trim();
    if (mmText.length() == 0) { mmText = "帮助"; }
    if (mmText.length() > 8000 || (body.conversationType + "") != "1") { return "麦米仅接受私聊文字"; }
    try {
        var mmCheck = mmText == "回调自检" && MmSystem.getenv("MAIMI_IM_QA_DIAGNOSTICS") == "1";
        if (mmText == "回调自检" && !mmCheck) { return "QA 回调自检未启用"; }
        var mmHasStamp = header != null && header["timestamp"] != null;
        var mmHasSign = header != null && header["sign"] != null;
        var mmSummary = "timestamp_present=" + mmHasStamp + ";sign_present=" + mmHasSign + ";verification=";
        if (mmCheck && (!mmHasStamp || !mmHasSign)) { return mmSummary + "MISSING_HEADERS"; }
        var mmEnv = MmSystem.getenv("MAIMI_IM_CALLBACK_MODE");
        if (mmEnv != "direct-https" && !mmCheck) { return "麦米接入未配置，未转发"; }
        // MagicAPI 2.2.2 内置 header 是本次请求头；不接受 body 中的签名。
        var mmStamp = header["timestamp"];
        var mmSign = header["sign"];
        if (mmStamp == null || mmSign == null) { return "麦米回调身份未验证"; }
        mmStamp = mmStamp + ""; mmSign = mmSign + "";
        if (!mmStamp.matches("[0-9]{13}") || !mmSign.matches("[A-Za-z0-9+/]{43}=")) {
            if (mmCheck) { return mmSummary + "INVALID_HEADERS"; } return "麦米回调身份未验证";
        }
        var mmNow = MmSystem.currentTimeMillis();
        if (MmMath.abs(mmNow - MmLong.parseLong(mmStamp)) > 60000L) {
            if (mmCheck) { return mmSummary+"STALE_TIMESTAMP"; } return "麦米回调已过期";
        }
        var mmSecret = "";
        try { mmSecret = Files.readString(Paths.get(MmSystem.getenv("MAIMI_IM_DING_SECRET_FILE")), StandardCharsets.UTF_8).strip(); }
        catch(mmConfigError) { if(mmCheck) { return mmSummary+"CONFIG_UNAVAILABLE"; } return "麦米回调身份未验证"; }
        if (mmSecret.length() == 0) { if(mmCheck) { return mmSummary+"CONFIG_UNAVAILABLE"; } return "麦米回调身份未验证"; }
        var mmMac = Mac.getInstance("HmacSHA256");
        mmMac.init(new SecretKeySpec(mmSecret.getBytes(StandardCharsets.UTF_8), "HmacSHA256"));
        var mmComputed = mmMac.doFinal((mmStamp + "\n" + mmSecret).getBytes(StandardCharsets.UTF_8));
        if (!MessageDigest.isEqual(mmComputed, Base64.getDecoder().decode(mmSign))) {
            if (mmCheck) { return mmSummary+"FAILED"; } return "麦米回调身份未验证";
        }
        if (mmCheck) { return mmSummary+"PASSED"; }

        var mmTarget = URI.create(MmSystem.getenv("MAIMI_IM_RELAY_URL"));
        if (mmTarget.getScheme() != "https" || mmTarget.getHost() == null || mmTarget.getRawUserInfo() != null
            || mmTarget.getRawQuery() != null || mmTarget.getRawFragment() != null || mmTarget.getRawPath() != "/internal/maimi-im/v1/dingtalk") {
            return "麦米服务地址未配置";
        }
        var mmHook = URI.create(body.sessionWebhook + "");
        if (mmHook.getScheme() != "https" || mmHook.getHost() != "oapi.dingtalk.com" || mmHook.getPort() != -1
            || mmHook.getRawUserInfo() != null || mmHook.getRawFragment() != null || mmHook.getRawPath() != "/robot/sendBySession"
            || mmHook.getRawQuery() == null || !mmHook.getRawQuery().matches("session=[A-Za-z0-9._~%+/=-]+")) {
            return "麦米回复地址无法验证";
        }
        var mmData = {"robotCode":body.robotCode,"senderCorpId":body.senderCorpId,"chatbotCorpId":body.chatbotCorpId,
            "senderStaffId":body.senderStaffId,"conversationType":"1","conversationId":body.conversationId,"msgId":body.msgId,
            "msgtype":"text","createAt":body.createAt,"text":{"content":mmText}};
        var mmRaw = JSON.toJSONString(mmData).getBytes(StandardCharsets.UTF_8);
        var mmAt = (mmNow / 1000L)::long + "";
        var mmNonce = UUID.randomUUID().toString();
        var mmHex = bytes => { var out = ""; for (one in bytes) { out = out + MmString.format("%02x", one & 255); } return out; };
        var mmBodyHash = mmHex(MessageDigest.getInstance("SHA-256").digest(mmRaw));
        var mmCanonical = "POST\n/internal/maimi-im/v1/dingtalk\n" + mmAt + "\n" + mmNonce + "\n" + mmBodyHash;
        var mmKey = Files.readAllBytes(Paths.get(MmSystem.getenv("MAIMI_IM_FORWARD_KEY_FILE")));
        if (mmKey.length < 32 || mmKey.length > 512) { return "麦米服务凭据未配置"; }
        mmMac.init(new SecretKeySpec(mmKey,"HmacSHA256"));
        var mmAuth = mmHex(mmMac.doFinal(mmCanonical.getBytes(StandardCharsets.UTF_8)));
        var mmFactory = new MmFactory();
        mmFactory.setConnectTimeout(2500); mmFactory.setReadTimeout(5500);
        var mmClient = new MmRest(mmFactory);
        var mmHeaders = new MmHeaders(); mmHeaders.set("Content-Type","application/json");
        mmHeaders.set("X-Maimi-Time",mmAt); mmHeaders.set("X-Maimi-Nonce",mmNonce); mmHeaders.set("X-Maimi-Sign",mmAuth);
        var mmAck = null;
        try {
            var mmReplyBody = mmClient.postForObject(mmTarget,new MmEntity(mmRaw,mmHeaders),MmString);
            mmAck = JSON.parseObject(mmReplyBody);
        } catch (mmForwardError) {
            // 已验证来源的私聊可收到明确的维护提示；绝不重投员工命令。
            mmAck = {"state":"unavailable","reply":"麦米服务暂未就绪，本条请求未取得结果，请稍后查看。"};
        }
        if (mmAck.state == "duplicate") { return "OK!"; }
        if (mmAck.reply == null || mmAck.reply.length() == 0 || mmAck.reply.length() > 8000) { return "麦米响应无效"; }
        var mmAnswer = JSON.toJSONString({"msgtype":"text","text":{"content":mmAck.reply}});
        var mmReplyHeaders=new MmHeaders(); mmReplyHeaders.set("Content-Type","application/json");
        var mmSent=mmClient.postForObject(mmHook,new MmEntity(mmAnswer.getBytes(StandardCharsets.UTF_8),mmReplyHeaders),MmString);
        if (JSON.parseObject(mmSent).errcode != 0) { return "麦米回复结果未确认，请勿自动重发"; }
        return "OK!";
    } catch (mmError) {
        return "麦米请求未完成，请联系管理员；不要自动重试。";
    }
}

