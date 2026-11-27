"""Cache only exact reviewed rc.2 program bytes, never runtime configuration.

DSH revision names hash file metadata, not content. Therefore revision alone
never establishes cache eligibility. Every returned segment and its generated
trailer must match the locally reviewed source pins after upstream auth.
"""
from __future__ import annotations
import hashlib
import re
import time
from dataclasses import dataclass
from urllib.parse import unquote
from .iam_oauth import IAMFailure

PINNED_SEGMENTS = {'@deepseek-ai/dsh-api-gateway': ((82153, '43e8d194b8a4c69554ee1325da3180d592a53ea562a6b888d65433566e90febb'),), '@deepseek-ai/dsh-api-job-controller': ((11997, 'c4e828ba43eaeef039e18e9e10c3e63d43fee283f4e51f0ab501198afb9f26b3'),), '@deepseek-ai/dsh-api-remotes': ((494248, '0555fd8520dbb7ebccfc963adaa3ff24dc089455c01f7aa02cec20bea4363009'),), '@deepseek-ai/dsh-api-session-controller': ((144260, '384e4deda623d290a269301c8d0deae3890af485179ce61645e15e8ebd7d173b'),), '@deepseek-ai/dsh-api-terminal-controller': ((32667, '922eb9e142032fe5db66bbe05dac1750af05d8c5979a5097b000452fefe3c95d'),), '@deepseek-ai/dsh-api-workspace-controller': ((20506, 'dc1b827787f5cc5634815ee0c6fec5c861ff8068de450cde3febb1565664bc6a'),), '@deepseek-ai/dsh-api-workspace-files': ((15853, '7b9a4a64a7a568d6fef953daa08e0437fe614b215932798cfd30a4877170762f'),), '@deepseek-ai/dsh-client-connection': ((59378, '07e7dda46c6da2aff6c5bc297f1e1c0c6484569b5416e9212fa44dfdda3390b6'),), '@deepseek-ai/dsh-client-file-upload': ((11669, 'c698754fac86b389bf328b02b0f22d594218670697fcc15895dc4859f9860074'),), '@deepseek-ai/dsh-client-hmr': ((3182, 'c4688fce7ffcfe79581ef11bc6dedea986c24d088e253003c6871f2a73ae7a41'),), '@deepseek-ai/dsh-client-locale': ((62920, '9a31e20e620a237e698719a8aea43130bc6da956fad3b7441a92eb9ac9996a1f'),), '@deepseek-ai/dsh-client-modules': ((40244, '41bece56566697537f61411b92e14b016ff07278db369400a6a4ced7ea18ef50'),), '@deepseek-ai/dsh-client-resources': ((5779, '9e787946e73d4643c0e27027e1f9f93ba6df4363864af98c678729e5b7706610'),), '@deepseek-ai/dsh-client-shortcuts': ((79918, '14bc807d4396a1b13d2d981156d40655d214519222fa97dca25da0c436a3c4c8'),), '@deepseek-ai/dsh-client-ui-agent-preset': ((84204, '75cbad1b97283dc85e0a8dc6b9d4da3da6c893dbb368e2b3717d81a12f70665a'),), '@deepseek-ai/dsh-client-ui-approval': ((14356, '6e9db75187e9531bb184237c187239b2c170ea518a7c69d7ef0a7bae61ffc4ae'),), '@deepseek-ai/dsh-client-ui-attachment': ((45031, '9b1613b96bcd710530402a1bd3a181094e15169b1385fffbb5b7213e3ed9ddac'),), '@deepseek-ai/dsh-client-ui-brand-official': ((1795966, '643d9bd964958c2b76ffab29a185b1b5a2be6ffd7ab697bda65f1b179ff53652'), (1800372, 'b566fc78e772ed386680075cb8a3ab28e29e35c208b48d0d766ce5b42c536ea5')), '@deepseek-ai/dsh-client-ui-chat': ((530915, '68f9d249414bf263a55b319cc187da75d71e530859f221576412791135fe9142'),), '@deepseek-ai/dsh-client-ui-commands': ((56511, '30df7d9e113ffcabd3a14b30a00c0f5d57f169535d584f5a40b9483a22bcc4fb'),), '@deepseek-ai/dsh-client-ui-conversation': ((715243, '555607cade16bcec33189eb5b49cb51cd38a0c912d119f2837f6665f25943a37'),), '@deepseek-ai/dsh-client-ui-cordis': ((75452, '4bf34dfaa6d6fb25673d38780b94cdcf09179cdda75e866939f7ec7e47920b52'),), '@deepseek-ai/dsh-client-ui-deliverables': ((110177, 'fd953bf8cc59ad278696d2438ee864474ccec8dec7984f2ee874d72487edf9d0'),), '@deepseek-ai/dsh-client-ui-directory-picker-browse': ((49642, '7562136bf5b0f58ce343a8a603380e83438286ad254d0246ab0ce6156b76c6f0'),), '@deepseek-ai/dsh-client-ui-directory-picker-native': ((2997, 'a2fbc26b801d97795ff848b43b8fccb51774e6474f5ca3020b1f1cafaf072ca0'),), '@deepseek-ai/dsh-client-ui-goal': ((25954, '5c5e3161ee0698d7e5e8ebc0ee1b05dfa5f7e24a535e9136106d000393456898'),), '@deepseek-ai/dsh-client-ui-input-trigger': ((55034, 'd33a5761e30ced8ef0e9f9b3f4c52bde6137bbb15e5d0c79dc3ae6d25d64b37f'),), '@deepseek-ai/dsh-client-ui-jobs': ((31758, '8487d519a591f31486ecbcb4ecda35607f2cf2e51f96f7d43ee0fed7283ebbc7'),), '@deepseek-ai/dsh-client-ui-layout': ((31301, '6e46a8cc6bf2872bde0994ba73990fc609d59fbed01c1f20fbf64913674280d6'),), '@deepseek-ai/dsh-client-ui-message-feedback': ((34379, 'd59d052017a1ef20838eea41b459015e8e6f23c491ecb5cdc928f7088175caa4'),), '@deepseek-ai/dsh-client-ui-model-selection': ((50332, '39c9b1678dcc3296d9e46637f74283e73dc01023d86097d153261f1dc3e333ae'),), '@deepseek-ai/dsh-client-ui-open-in-app': ((33837, '5cf9897bfa64bc66eca3bba8c1e1fad660d28315ecab0938c7a08148ff05add2'),), '@deepseek-ai/dsh-client-ui-permission-presets': ((37489, '874627f41b0709409a58f85338ef446ceb9921f5e59a0bb3ebcd9463c4be1eb7'),), '@deepseek-ai/dsh-client-ui-plan': ((31443, '6caa9ca638f899aba0158f7b279415b74ad8a0bbd6ef569b3069825200cc8091'),), '@deepseek-ai/dsh-client-ui-plugin-manager': ((168363, '49c33a05abfba6aaf2ea0e088bb85f4c4f5bce1819620eb387937ef0a76f457d'),), '@deepseek-ai/dsh-client-ui-reference': ((13560, '901f17986118522cd509fb6e7156b026a46eff45c13369d31955735ef6209a45'),), '@deepseek-ai/dsh-client-ui-renderer': ((75877, '32b38669b86f49b4d413604828d3efe6aa93b7aa2a11d849ae52902ce1613bda'),), '@deepseek-ai/dsh-client-ui-schedule': ((304920, '2602ee1448142536284538fbc1457fa00c9119bde4a25f9f9f74af7d786d0f9c'),), '@deepseek-ai/dsh-client-ui-session': ((18507, '454d0e0afe6d84f2d3419fe552daa82c8a7165b256de37af472f12e14d79591c'),), '@deepseek-ai/dsh-client-ui-settings': ((59109, '6938fc788c4d05268049395784bce13f12a0f929ab0bfec73e80b34622a2946e'),), '@deepseek-ai/dsh-client-ui-settings-account': ((5276922, '5865f82c487128253dfe4329706fce69b9381262e7ac9aa22f33034708cf8db8'),), '@deepseek-ai/dsh-client-ui-settings-agent-loop': ((6207, 'fd58d47605a4feeae2940b0b97488c2ff7584b3282dccdf095dc6b0bebd9b0e1'),), '@deepseek-ai/dsh-client-ui-settings-general': ((52302, '91e43d19be7849b90983362e57a461cb4800f5ae3a98909cf309661a031d9a3d'),), '@deepseek-ai/dsh-client-ui-settings-models': ((185968, 'fed36bda20d6674c6bfc6fe587271f2dfeab8c946886adef38234407599d6ece'),), '@deepseek-ai/dsh-client-ui-settings-plugin-inventory': ((40835, 'a15c1c5cbed14946c9318e4a214576b7f81da07f7c11d78f30f47cdc9bd9e991'),), '@deepseek-ai/dsh-client-ui-settings-plugins': ((9706, '04edda530de4f09b2fbcefd87bcca98be15c458b74db2fa33654ab24322389c7'),), '@deepseek-ai/dsh-client-ui-settings-shell': ((7271, '3263e34775cbac3e868c8d690b99633f8a9f9a4d233eca6cc8bb4552b5b77f47'),), '@deepseek-ai/dsh-client-ui-settings-subagent': ((38444, '5ec9c4415dbba300eb77a3cfc223edf9f35f0e5834d90caf883326d6af8ab80c'),), '@deepseek-ai/dsh-client-ui-settings-web-search': ((11713, 'b4d6f2985065a4fd99c2fb8c9deaabff57c4b4875e5c20f2e6f2c5a27092f4fc'),), '@deepseek-ai/dsh-client-ui-shortcuts': ((55398, '5ea197fe2c7c6e770b2f96d1f3f81d563f54b8619a4f5d9089ec76a970593c59'),), '@deepseek-ai/dsh-client-ui-sidebar': ((32019, '0c65fc7d80af2c28a95cf723e02e9fbab1baaeee76039628ef90b5267c48e12c'),), '@deepseek-ai/dsh-client-ui-sidebar-browser': ((61517, '3ecc34f04ed36bdd0b4d786bfaf6f7868e6dff91b3ca74c520ccabd9dec95cfa'),), '@deepseek-ai/dsh-client-ui-sidebar-documentpreview': ((269081, '89b89552ac8c8f17e3af1ec71f927922bdcc89354cb5f277b4059a7ffb601b0a'),), '@deepseek-ai/dsh-client-ui-sidebar-files': ((40135, 'e330f07bcd899a80bfbbfc30420b0edf43c7775dcc087d10ba33e22014d04d45'),), '@deepseek-ai/dsh-client-ui-sidebar-right': ((330051, '86fd5fd99844d454e608770a457a0d76f6d1922ab8ffc344c3bbc0946df8375a'),), '@deepseek-ai/dsh-client-ui-sidebar-terminal': ((19866, 'eebb2fec76481e8220110c472b7c54a9db3170451b8c4962ad6cdff602363818'),), '@deepseek-ai/dsh-client-ui-skill': ((21877, '08519a3683c3c3fb2e004f1d3c2e4808c400bf6b787d70eea0da25721b7d25a4'),), '@deepseek-ai/dsh-client-ui-subagent': ((48572, 'c498d0a907385246dd961efaeb99a170b6a46e09907cebce64e4555f90f9cfd5'),), '@deepseek-ai/dsh-client-ui-theme': ((100503, '364315de764c1b55e56938ab787295b61f173203d67da0b31509fd3d968aaf8e'),), '@deepseek-ai/dsh-client-ui-tool': ((182898, '2a7a1e1956b3d53d0eb0cc04a32bd71f4363bd5dc788bc2826e2ee2420328a29'),), '@deepseek-ai/dsh-client-ui-trajectory': ((421616, 'b68da419d5b1871213ecc447812d943d7b206f6fa92f1e3cca1289c7420426da'),), '@deepseek-ai/dsh-client-ui-user-questions': ((43240, 'c60db04f991de1e388658fc4821d40a8e5392265a6a7abe7b6a5f21308ba4a19'),), '@deepseek-ai/dsh-client-ui-workflow-run': ((29651, 'ace56a6c7d004c85d7c8bd4b84de00897abc430bd95f0df47d17fe1f374163bd'),), '@deepseek-ai/dsh-client-ui-workspace': ((202567, '057eae003c8504994220607b0d64791fbef977b13f0c979898c615bbae71f6d6'),), '@deepseek-ai/dsh-cordis-client-runner': ((350132, '91aec1f3d00112b0c4d1dda53deeaf336b765990d1f9a4b39bdd4ef4c9800daf'),), '@deepseek-ai/dsh-experimental-agent-team': ((110, '010531e19924927c47110eac080c39a2f8a2a2157f3897f98fc96ba2201dd5fa'),), '@deepseek-ai/dsh-experimental-client-ui-agent-team': ((25357, '864b118fd153c3b8cab17adf58eec5add914f7820c8ee57ced6ae3e9158aa953'),), '@deepseek-ai/dsh-experimental-client-ui-voice-input': ((212227, 'cfc2e680646f02431b1b4adb8fff17a156bbb293cbd748b96bd37e2903dad1c7'),), '@deepseek-ai/dsh-goal': ((348, '7ccc9fd5b6b7c09415ebce66490a55e66a2c061bf9f6efeeadde7700ca8c9347'),), '@deepseek-ai/dsh-permission-presets': ((368, 'e7c6d0f79d8f50d9ae575c51972e8b4fea1d324d2481f637362aebf4ad9f393f'),), '@deepseek-ai/dsh-plan-mode': ((353, 'c3bc2a35d397b64c580a402e83dc04336ac9b670ebe717a85c3b6a7e319b2634'),), '@deepseek-ai/dsh-schedule': ((13, '6772232d5095e29e44e5a6fc9b446329d3c90e54f6a5e71be91b90cc3de0171d'),), '@deepseek-ai/dsh-session-log-export': ((12439, 'f9346234213d5083a49dbd39f8717944e7eb71bf99e2c6b7a77cbfb2241b823a'),), '@deepseek-ai/dsh-session-stats': ((366, 'd58ad3d35fd558acfe833448e0994d8fdd20fdabedc3ae9644e7588c45daa249'),), '@deepseek-ai/dsh-session-title': ((358, 'b7fdefb013654e4554d91b23350ad9e34fa7c3056e86c58c7ddb1e03705c724a'),), '@deepseek-ai/dsh-session-turn-outline': ((372, 'c7c1d91bc510a601dd1548525dc0a8860601102c6e942b83e5bc06fb58a164e0'),), '@deepseek-ai/dsh-subagent': ((128, 'e05c9e9c776b0e464418e7f5f2d88053f7c17f4598e2f26b7602ab3a4c9a490b'),), '@deepseek-ai/dsh-token-meter': ((198, '53566e191f6355b24160d70ac0e861c4f265fa0a42e9d459101b98251ff8789f'),), '@deepseek-ai/dsh-tool-todo': ((353, '61bbf7c896f325675b24985c5822f23727e008feb0835691615e9e9a9eda49c2'),), '@deepseek-ai/dsh-typert-registry': ((52866, '58826d31c34ee4a2e7d515e0b3a94c4923dae5536d3834ef70fadf746c9e752c'),)}
# R47 fixed program bytes only; native configuration and employee data are never cacheable.
PINNED_SEGMENTS['@deepseek-ai/dsh-client-ui-brand-official'] += ((1826556, '27a63ac6515e8ed0ed172023b3ab64cf41e293ecd35431131638eb5e8cbf4115'),)
# R48 reviewed immutable frontend only.
PINNED_SEGMENTS['@deepseek-ai/dsh-client-ui-brand-official'] += ((1830353, '6841c24eac8134ca7a91b89ec610c3f296f66f0cfd9ab0d331737b00db0886c7'),)
# Exact frozen R50 frontend reused for R52; final review pending.
PINNED_SEGMENTS['@deepseek-ai/dsh-client-ui-brand-official'] += ((1828013, 'b7339c2e01ec26eaa28bc506ac3878b8d04776564f24ebddd70f7a987a8071d4'),)
# Fixed browser programs in the admitted 5167751957c1 worker image.
for _module_name, _program_pin in {
    '@deepseek-ai/dsh-api-gateway': (53079, '028ee377641e4dddae22c60b706303ee0540e106887785e248a94a3ae3d723b1'),
    '@deepseek-ai/dsh-api-job-controller': (6067, 'c6566fc724ae63d693d3f4a27a324e78a2a8a4cfa50d719886d856948cf8249d'),
    '@deepseek-ai/dsh-api-remote-access': (15689, '5ed83faad07716a326fc5d225bcf93d05755fc27ffc7376a13a31461a23d839d'),
    '@deepseek-ai/dsh-api-remotes': (1161067, 'd5d8d0417edf8861271bd28385a44a18390f78f3c638c9fdd4eabd2e60ff1ec6'),
    '@deepseek-ai/dsh-api-session-controller': (94661, '28b68761b6a4f73f08cfa34bd01e002cba40181b942239802d4c0b3442377ad3'),
    '@deepseek-ai/dsh-api-terminal-controller': (27567, 'e8349bc498db5a59222c9781dcfa14286274c996f37c93728fcca7eba6247f28'),
    '@deepseek-ai/dsh-api-workspace-controller': (12105, 'ee27e3d26c11c7bb7213679a987cc1faa7c517d8e71fe8bfce9c86d052ba33fc'),
    '@deepseek-ai/dsh-api-workspace-files': (6757, '988bce09d3c3818a855c930000f59dc7c6dd35a2cd4555570751a426a5dbdec2'),
    '@deepseek-ai/dsh-client-connection': (43951, 'a9c309483b8977c591bf10206007551764867df877350e74fc8dd857246d1f52'),
    '@deepseek-ai/dsh-client-file-upload': (8026, '776f9b67928498f2a1c77e96230a70a2c261a58de08342d49c474ba2968841fc'),
    '@deepseek-ai/dsh-client-hmr': (8931, '27da7c5a2fcd734d5d9be0341719e4ae7539eeca793cdacd041bc8b2904d6c08'),
    '@deepseek-ai/dsh-client-locale': (50834, '3c0a4e683dd3fde460fac77dc01ae51cb5608d46cc8523d63efa85d4bc9579bd'),
    '@deepseek-ai/dsh-client-modules': (25799, '76100984f90134e037370c14343d08f7c426e6791ca696ec75584047cf9388d1'),
    '@deepseek-ai/dsh-client-resources': (3631, '26efd3a358ef5b241bd4bd7ac564b0bc5e7c144eb4cc2a2ba7d14337c976f5c1'),
    '@deepseek-ai/dsh-client-shortcuts': (66753, '3d7a4db26257dc4118366b4ca0e304dc16ff479e568d225176d2bffde5f7dfe3'),
    '@deepseek-ai/dsh-client-ui-account-notifications': (42807, '6bfecfa5967eaf8ac36570cbaf3f728d13b546be1982470935a6fd4480a1f033'),
    '@deepseek-ai/dsh-client-ui-agent-preset': (147615, '4dc49b4e39beb487dd0bb1e4ccbd6a523894b68c18a13fc6c865d6c09dbc3174'),
    '@deepseek-ai/dsh-client-ui-agent-team': (45454, '1988fac692e8a714639535635bb3edd703453bb876ef07fc2bdc0500cf1b93fe'),
    '@deepseek-ai/dsh-client-ui-approval': (19602, '59a3e456fb4160580ed20aa32c693cecbe5f8a98b0cd4e3c6853f42881724fcf'),
    '@deepseek-ai/dsh-client-ui-attachment': (77570, '205dbf5341c0b4cf47bc78439a70c8f6f1d117430924ad98f9413c32697ad766'),
    '@deepseek-ai/dsh-client-ui-chat': (509656, '50db45aefc11ae05c26d25734f29860b0cad7c39a0485ffa3c803549654bb983'),
    '@deepseek-ai/dsh-client-ui-commands': (53365, '87851e3257e2150f7b8f576cf4b1b93b5a16e09910d16c6d7f45726536ff1d6f'),
    '@deepseek-ai/dsh-client-ui-context-menu': (43469, 'ff18de633b7c770fd10558f909ee25d8d699d97e7e7d916e0ef04cad13bc116c'),
    '@deepseek-ai/dsh-client-ui-conversation': (704151, 'c3bb1c6047f6d86f1744d9ad5f2f845769e5bb0804f36acdb133d1edeb233876'),
    '@deepseek-ai/dsh-client-ui-cordis': (72307, '9db387bded3354ff1b6800ba601272b2903b2d936284929e39188e4f308c7982'),
    '@deepseek-ai/dsh-client-ui-deliverables': (92856, '981562905d3a504c757243b70a0469aee57d190063ff81435d4ecf7f0ade9678'),
    '@deepseek-ai/dsh-client-ui-directory-picker-browse': (33009, 'e74ad3b3678b3af0fdda28bacbb758877d719d57b84be7ae33161eb336128db6'),
    '@deepseek-ai/dsh-client-ui-file-actions': (55326, 'd8df63aaca17ee2ad78a7bfec282323a967ab016ce2af71ca09693dd107bca05'),
    '@deepseek-ai/dsh-client-ui-file-drag': (48450, 'a56349be52a10be80c8f4bb5a7ea7a2a0efb437e5469b0845e77ba401b701104'),
    '@deepseek-ai/dsh-client-ui-goal': (21080, '5e0043fe0b1734faac9adcf17f4b55b696e3a8df0f47c2671cd0a0b365630c96'),
    '@deepseek-ai/dsh-client-ui-input-trigger': (44793, '1d15964fbc5f495ed3520e4dfa85524f406edc4d919414627d11f851c0cd41f1'),
    '@deepseek-ai/dsh-client-ui-jobs': (39121, '0dec8a8a1507a2efd931ec3dcd1c793820c156f51a294efaacd05997a12f514a'),
    '@deepseek-ai/dsh-client-ui-layout': (25851, 'f46e95e6eb58d41c9bcab9fbedb36200f6269d1f4060bd2a48ff3664475d6ba2'),
    '@deepseek-ai/dsh-client-ui-message-feedback': (28410, '3c882e94da223644cddaa76fd8534029f64df84e2f729c7ef56fa6ce6436f8e1'),
    '@deepseek-ai/dsh-client-ui-model-selection': (61951, '7ce5e21e64c72c7b4872f32ea6227b49c98b5eadaad3ae31fd046feeb390fb4a'),
    '@deepseek-ai/dsh-client-ui-permission-presets': (40829, '47eff04fabe8e62878b67ba57a3c27b2ccf54590678039baebf9446e36da4e7d'),
    '@deepseek-ai/dsh-client-ui-plan': (25869, '5c2e48f0fc7b2d7dcd0e69ad85265912cd74d55f21194e0cbdf47d32335e25ca'),
    '@deepseek-ai/dsh-client-ui-reference': (14736, '20afa06ccda87d77a906797b6980da73756ecf0b222549551462814310abb1a9'),
    '@deepseek-ai/dsh-client-ui-renderer': (53714, '0e527f7679473dbfa949811fa688f369df4a302efbad06f55fdc83ea9aabf429'),
    '@deepseek-ai/dsh-client-ui-schedule': (550308, '1e3f867ca751159484d28d4f609a1d6fcab57fb60b06db0213ce2d92c1ae18af'),
    '@deepseek-ai/dsh-client-ui-secret-mask': (18294, 'c85a9b5e21ed770ec3f0a217fa5422c5732dc282c5e36bbd6d458b9b6cc877d2'),
    '@deepseek-ai/dsh-client-ui-session': (24703, '2d5965422c780aa516fd0c227a39ca099aea09a00b079b0daee987fedd139f56'),
    '@deepseek-ai/dsh-client-ui-settings': (36993, 'bfb08a912bdfc381e47ee9d34ddab7496f3400b641e1b68d318e4ccf24a16acf'),
    '@deepseek-ai/dsh-client-ui-settings-account': (187303, '46aeb3855757579abaaf1c730aecd69745e3ce7380f84ad3c3acfd4df1cc7cfa'),
    '@deepseek-ai/dsh-client-ui-settings-agent-loop': (8572, '7de37ae2eb1846526c26b152503c236cb6a61791dce652f7aeb0a963cfcc2c7f'),
    '@deepseek-ai/dsh-client-ui-settings-models': (245238, '418cd578be2cc8270e225f8a95b42b5851da5ce2ca6e5e3fb750216bef4c094a'),
    '@deepseek-ai/dsh-client-ui-settings-plugin-inventory': (42706, 'eddb645c26ba04781f8816e3a9d78e2a4b678cf636a10bb02783f05156e74beb'),
    '@deepseek-ai/dsh-client-ui-settings-plugins': (8467, 'b486e145ae1152148755a842a1f102dddc1fee59a9d38d331fdbe8db2425fa3b'),
    '@deepseek-ai/dsh-client-ui-settings-session-log': (5597, '22fe4b6bebb46894c88b3af8044dc6a8f9c66546cd3416e228fb809c861af023'),
    '@deepseek-ai/dsh-client-ui-settings-shell': (10305, '6a6ff60b362b830aaa365f776caf8572fc007c9f090fe8767647e219d7699e7f'),
    '@deepseek-ai/dsh-client-ui-settings-subagent': (46726, '92f280db87dd480b0f989eae9936076a18f9b5aca59ac170837ec5bc70619b5d'),
    '@deepseek-ai/dsh-client-ui-settings-web-search': (46456, 'a39e9ea11ec9353f117a3483765ae3b79f1bd533bd9bd004007aca15c1b5d91b'),
    '@deepseek-ai/dsh-client-ui-shortcuts': (69324, '38695a0adb51b8569de4cd71ee9ced3215f0f5005571af400d1528ad8a0cc377'),
    '@deepseek-ai/dsh-client-ui-sidebar': (41855, 'fa88ce77d0960b55f77eec8cc4ef0de8e785a492f170c3630ed9d6a84933c899'),
    '@deepseek-ai/dsh-client-ui-sidebar-documentpreview': (217903, '945ae78b7f10ef09014a8111818bcba9d6f0c52fcf6030f84a3e422593df4500'),
    '@deepseek-ai/dsh-client-ui-sidebar-files': (109546, '3d25c7d78309f0b585c817e9e71a01cd38ba57e7949064d4373151f72565c8ce'),
    '@deepseek-ai/dsh-client-ui-sidebar-right': (140948, '3dd68136272d64acc0ebb8a5b1e3912e7c1ee5dec4c501a85eaf34688e057f64'),
    '@deepseek-ai/dsh-client-ui-sidebar-terminal': (30694, '85888849175b525d5e9a9acc8397b9141774feac58615ef9359f09914ba80806'),
    '@deepseek-ai/dsh-client-ui-skill': (19171, '23b2a464b9417abcd4d301faca51aa6d1fe471db69a30bc757cc8fa404de84be'),
    '@deepseek-ai/dsh-client-ui-subagent': (60261, 'cd4810e9250550adc3978e67093a7142b83eb885acbaf00e25fcf4bf3ba91735'),
    '@deepseek-ai/dsh-client-ui-theme': (113281, '21953c251197822a5ef31d162aef189ae092f6328ffd5cea8420434e6d57c2af'),
    '@deepseek-ai/dsh-client-ui-tool': (151218, '5b3572585108ef57df8abd799159bef7e859e9e3a1d0ecdcd2b1d194fc694937'),
    '@deepseek-ai/dsh-client-ui-trajectory': (370551, 'd629defccef983225a029c1c44ee99016408d82495a6d50eca5154e95c8b8274'),
    '@deepseek-ai/dsh-client-ui-user-questions': (75554, '6538b35c0183a707d65a0ec44ce91991f0c3ee40f9a7423fa7bd102e93c04168'),
    '@deepseek-ai/dsh-client-ui-workflow-run': (28098, '5b8ffd02b104d286e65b6e282562a05fa8c105bbef63f2102c5fda6c83e68579'),
    '@deepseek-ai/dsh-client-ui-workspace': (204986, 'add77bd820e1d086a97536342059616141a2011cf6d23d09ddce6d7887466cf2'),
    '@deepseek-ai/dsh-cordis-client-runner': (385688, '7bce4c2af1679a21fdcdcaef7894dc75c58738bed6831076e2221693480bc318'),
    '@deepseek-ai/dsh-message-quotes': (4304, '482b0d7ad329ee05eac0a7510e01215f0b60b27260485a479ed38db045a0585e'),
    '@deepseek-ai/dsh-session-log-export': (18536, '2acb76a61b5ce23f02f8b16231dbffd5d2ce8930397a39ed0e9d944c5bbb9b67'),
    '@deepseek-ai/dsh-typert-registry': (17698, '89eb02c4e4e081e5f2b6a99f6b8378e88424c791105756b0e444b82799b78250'),
}.items():
    PINNED_SEGMENTS[_module_name] = (*PINNED_SEGMENTS.get(_module_name, ()), _program_pin)

MAX_BYTES = 16 * 1024 * 1024
QUERY = re.compile(r'\?(?P<files>@deepseek-ai/[a-z0-9-]+/client\.js(?:,@deepseek-ai/[a-z0-9-]+/client\.js)*)&rev=(?P<rev>[a-f0-9]{12})\Z')

GENUI_PREFIX = '/plugins/@changfenhuang/dsh-genui/assets/'
# Exact npm @changfenhuang/dsh-genui 0.14.0 bytes, source-lock.json.
GENUI_ASSETS = {
    'echarts-core.js': (588213, '5eed01ed23a0246559d14f59c2ff5fc1b4fcca13558c2ed763a73724cd2b01e0'),
    'echarts-full.js': (1044998, '53914c2d4957bf29614738f34ce14698ae6d677e5d595a88b79fadef2881c644'),
    'icons.js': (969068, '1b2bb5c7256fb28de549cf0f43fa908dc8ae674ed4d6d13006909f9995aa1fe1'),
    'mermaid.js': (3394519, '5a728583401b2146925e857ae5cb234ec1554276e7a6bbb8bf08f62ec150dc99'),
    'standalone-runtime.js': (1140137, 'b1bae855ee25e4cee79a5f777a1f16a1bcb3a232ad9ca973bdb8065f0651f9a0'),
    'three.js': (699985, '3b61ad28030d938519dd52db05152690ef01c58f17bfdb5cd8c4579b25e1c7a9'),
}

# R93: exact GenUI 0.15.0 program bytes alongside existing 0.14.0 workers.
GENUI_ASSETS.update({'maplibre.js': (1665370, '895cb7463511a787dd6e742195744011e54b676253540fbe572a67cfc6350180')})
GENUI_ADDITIONAL_PINS = {'mermaid.js': ((3395203, '1bf1325a88367723401c6bb8735b39f04a99fced074eec7d13ea8c8146905eda'),), 'standalone-runtime.js': ((1190721, '8a5525258e6c2f320b8461b3f969ecf9518d16d0f1fe2bb3e8396d8cf50d02ce'),)}

@dataclass(frozen=True)
class FixedPluginAsset:
    size: int
    sha256: str
    versioned: bool
    alternatives: tuple[tuple[int, str], ...] = ()

def plugin_plan(method, path, query, headers):
    if path.startswith('/plugins/@changfenhuang/dsh-genui') or unquote(path).startswith('/plugins/@changfenhuang/dsh-genui'):
        pin=GENUI_ASSETS.get(path[len(GENUI_PREFIX):]) if path.startswith(GENUI_PREFIX) else None
        if (pin is None or method not in ('GET','HEAD') or headers.get('range')
                or (query and re.fullmatch(r'rev=[a-f0-9]{12}',query) is None)):
            raise IAMFailure(404,'personal_cloud_plugin_asset_invalid')
        return FixedPluginAsset(*pin,versioned=bool(query),alternatives=GENUI_ADDITIONAL_PINS.get(path[len(GENUI_PREFIX):],())) if method=='GET' else None
    # Only the official combo script route; source maps, chunks, extension
    # URLs, encoded aliases, partial requests and owner routes remain no-store.
    if method!='GET' or path!='/plugins/' or len(query)>3072 or headers.get('range'):
        return None
    match=QUERY.fullmatch(query)
    if match is None:return None
    files=match.group('files').split(',')
    ids=tuple(value[:-len('/client.js')] for value in files)
    if len(ids)>100 or len(set(ids))!=len(ids) or any(value not in PINNED_SEGMENTS for value in ids):
        return None
    trailer=('//# sourceMappingURL=??'+','.join(value+'.map' for value in files)+'&rev='+match.group('rev')+'\n').encode('ascii')
    lower=sum(min(x[0] for x in PINNED_SEGMENTS[value]) for value in ids)+len(trailer)
    upper=sum(max(x[0] for x in PINNED_SEGMENTS[value]) for value in ids)+len(trailer)
    if upper>MAX_BYTES:return None
    return ids,trailer,lower,upper

def exact_plugin_body(body,plan):
    if isinstance(plan,FixedPluginAsset):
        return any(len(body)==size and hashlib.sha256(body).hexdigest()==digest
                   for size,digest in ((plan.size,plan.sha256),)+plan.alternatives)
    ids,trailer,lower,upper=plan
    if not lower<=len(body)<=upper:return False
    offsets={0}
    for name in ids:
        offsets={offset+size for offset in offsets for size,digest in PINNED_SEGMENTS[name]
                 if hashlib.sha256(memoryview(body)[offset:offset+size]).hexdigest()==digest}
        if not offsets:return False
    return any(body[offset:]==trailer for offset in offsets)

async def read_verified_plugins(response,plan,validate):
    body=bytearray();checked=0
    async for chunk in response.aiter_raw():
        body.extend(chunk)
        if len(body)>(max(size for size,_ in ((plan.size,plan.sha256),)+plan.alternatives) if isinstance(plan,FixedPluginAsset) else MAX_BYTES):raise IAMFailure(502,'personal_cloud_static_response_invalid')
        if time.monotonic()-checked>=5:
            await validate();checked=time.monotonic()
    await validate()
    mime=response.headers.get('content-type','').split(';',1)[0].strip().lower()
    if isinstance(plan,FixedPluginAsset):
        if not (response.status_code==200 and mime in ('text/javascript','application/javascript')
                and response.headers.get('content-encoding','identity').lower()=='identity'
                and exact_plugin_body(body,plan)):
            raise IAMFailure(502,'personal_cloud_static_response_invalid')
        return bytes(body),plan.versioned
    eligible=(response.status_code==200 and mime=='text/javascript'
              and response.headers.get('content-encoding','identity').lower()=='identity'
              and exact_plugin_body(body,plan))
    return bytes(body),eligible
