#include <ctype.h>

#include "fmtline.h"

/* upper-cases the first 26 bytes of a report detail line; returns the count changed. */
int fmtline(char *line)
{
    int changed = 0;
    for (int i = 0; i < 26; i++) {
        if (islower((unsigned char)line[i])) {
            line[i] = (char)toupper((unsigned char)line[i]);
            changed++;
        }
    }
    return changed;
}
