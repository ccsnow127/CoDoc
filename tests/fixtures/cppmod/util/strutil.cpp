#include "strutil.hpp"

namespace util {

// trim removes leading and trailing spaces and tabs.
std::string trim(const std::string &s) {
    std::size_t a = 0, b = s.size();
    while (a < b && (s[a] == ' ' || s[a] == '\t')) {
        a++;
    }
    while (b > a && (s[b - 1] == ' ' || s[b - 1] == '\t')) {
        b--;
    }
    return s.substr(a, b - a);
}

// split cuts s at every sep; an empty input gives one empty part.
std::vector<std::string> split(const std::string &s, char sep) {
    std::vector<std::string> out;
    std::string cur;
    for (char c : s) {
        if (c == sep) {
            out.push_back(cur);
            cur.clear();
        } else {
            cur += c;
        }
    }
    out.push_back(cur);
    return out;
}

// join concatenates parts with sep between them.
std::string join(const std::vector<std::string> &parts, const std::string &sep) {
    std::string out;
    for (std::size_t i = 0; i < parts.size(); i++) {
        if (i > 0) {
            out += sep;
        }
        out += parts[i];
    }
    return out;
}

}  // namespace util
