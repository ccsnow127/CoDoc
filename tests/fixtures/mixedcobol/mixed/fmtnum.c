#include <stdio.h>

/* formats a count right-aligned in five characters; nobody calls it (yet). */
int fmtnum(int n, char *out)
{
    return snprintf(out, 6, "%5d", n);
}
