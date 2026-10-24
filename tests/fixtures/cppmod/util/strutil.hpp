#ifndef UTIL_STRUTIL_HPP
#define UTIL_STRUTIL_HPP

#include <string>
#include <vector>

namespace util {

std::string trim(const std::string &s);
std::vector<std::string> split(const std::string &s, char sep);
std::string join(const std::vector<std::string> &parts, const std::string &sep);

}  // namespace util

#endif
