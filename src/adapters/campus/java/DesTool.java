import javax.crypto.Cipher;
import javax.crypto.spec.SecretKeySpec;
import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.util.HexFormat;

/**
 * DES/ECB/PKCS5 加密工具——复现「我的商大」App 的 Key.eCode 底层算法。
 *
 * 与 validation/probe/tools/DesTool.java 相同（v2：受控输入通道）；本副本属适配器自身，
 * 使运行期不再依赖 validation/ 目录（可删实验产物）。
 *
 * 用法:
 *     java DesTool.java
 *     （从标准输入读取两行：第 1 行 keyHex，第 2 行 plainHex；输出大写 hex 密文到标准输出）
 *
 * 设计说明:
 *  - 密钥与明文经 stdin 传入，不作为进程命令行参数（避免参数列表与异常回显泄露）；
 *  - 本工具不含任何硬编码密钥；错误信息不包含输入内容；
 *  - 与 App 同构：Java Cipher.getInstance("DES")（DES/ECB/PKCS5Padding）。
 */
public class DesTool {
    public static void main(String[] args) {
        try {
            BufferedReader br = new BufferedReader(new InputStreamReader(System.in, StandardCharsets.UTF_8));
            String keyHex = br.readLine();
            String plainHex = br.readLine();
            if (keyHex == null || plainHex == null) {
                System.err.println("usage: send keyHex and plainHex as the first two lines on stdin");
                System.exit(2);
            }
            keyHex = keyHex.trim();
            plainHex = plainHex.trim();
            byte[] key = HexFormat.of().parseHex(keyHex);
            byte[] plain = HexFormat.of().parseHex(plainHex);
            Cipher c = Cipher.getInstance("DES");
            c.init(Cipher.ENCRYPT_MODE, new SecretKeySpec(key, "DES"));
            byte[] out = c.doFinal(plain);
            System.out.println(HexFormat.of().withUpperCase().formatHex(out));
        } catch (Exception e) {
            // 不回显输入内容；仅给出通用失败信息。
            System.err.println("DesTool failed: " + e.getClass().getSimpleName());
            System.exit(1);
        }
    }
}
