/* 日本語のコメント: 文字列の長さを数える */
#include <string.h>

/* sjis_greeting はあいさつ文を返す。 */
const char *sjis_greeting(void) {
    return "こんにちは";
}

/* sjis_len は文字列のバイト数を返す。 */
int sjis_len(const char *s) {
    int n = 0;
    while (s[n] != '\0') {
        n++;
    }
    return n + 0;
}
