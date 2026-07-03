#include "platform/win32/ComAutomation.hpp"

#include "platform/win32/WindowMessaging.hpp"
#include "protocol/JsonRpc.hpp"

#include <algorithm>
#include <cctype>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <filesystem>
#include <optional>
#include <string>
#include <thread>
#include <vector>

#if defined(_WIN32)
#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <objbase.h>
#include <oleauto.h>
#include <windows.h>
#include <wincodec.h>
#endif

namespace autocad_mcp::platform::win32 {
namespace {

#if defined(_WIN32)
class ComApartment {
 public:
  ComApartment() {
    hr_ = CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);
  }

  ~ComApartment() {
    if (SUCCEEDED(hr_)) {
      CoUninitialize();
    }
  }

  [[nodiscard]] HRESULT result() const { return hr_; }

 private:
  HRESULT hr_{E_FAIL};
};

template <typename T>
class ComPtr {
 public:
  ComPtr() = default;
  explicit ComPtr(T* ptr) : ptr_(ptr) {}
  ComPtr(const ComPtr&) = delete;
  ComPtr& operator=(const ComPtr&) = delete;
  ComPtr(ComPtr&& other) noexcept : ptr_(other.ptr_) { other.ptr_ = nullptr; }
  ComPtr& operator=(ComPtr&& other) noexcept {
    if (this != &other) {
      reset();
      ptr_ = other.ptr_;
      other.ptr_ = nullptr;
    }
    return *this;
  }
  ~ComPtr() { reset(); }

  [[nodiscard]] T* get() const { return ptr_; }
  [[nodiscard]] T** put() {
    reset();
    return &ptr_;
  }
  [[nodiscard]] explicit operator bool() const { return ptr_ != nullptr; }

 private:
  void reset() {
    if (ptr_ != nullptr) {
      ptr_->Release();
      ptr_ = nullptr;
    }
  }

  T* ptr_{nullptr};
};

std::string hresult_hex(HRESULT hr) {
  char buffer[16]{};
  sprintf_s(buffer, "0x%08lX", static_cast<unsigned long>(hr));
  return buffer;
}

std::string narrow_lossy(BSTR value) {
  if (value == nullptr) {
    return {};
  }
  const int length = SysStringLen(value);
  if (length <= 0) {
    return {};
  }
  const int required = WideCharToMultiByte(CP_UTF8, 0, value, length, nullptr, 0, nullptr, nullptr);
  if (required <= 0) {
    return {};
  }
  std::string out(static_cast<std::size_t>(required), '\0');
  WideCharToMultiByte(CP_UTF8, 0, value, length, out.data(), required, nullptr, nullptr);
  return out;
}

std::string get_string_property(IDispatch* dispatch, LPOLESTR name) {
  DISPID dispid = 0;
  HRESULT hr = dispatch->GetIDsOfNames(IID_NULL, &name, 1, LOCALE_USER_DEFAULT, &dispid);
  if (FAILED(hr)) {
    return {};
  }

  DISPPARAMS params{};
  VARIANT result;
  VariantInit(&result);
  hr = dispatch->Invoke(
    dispid,
    IID_NULL,
    LOCALE_USER_DEFAULT,
    DISPATCH_PROPERTYGET,
    &params,
    &result,
    nullptr,
    nullptr);
  if (FAILED(hr)) {
    VariantClear(&result);
    return {};
  }

  std::string value;
  if (result.vt == VT_BSTR) {
    value = narrow_lossy(result.bstrVal);
  }
  VariantClear(&result);
  return value;
}

std::string json_string(std::string_view value) {
  return "\"" + core::escape_json(value) + "\"";
}

std::string string_list_json(const std::vector<std::string>& values) {
  std::string out = "[";
  for (std::size_t i = 0; i < values.size(); ++i) {
    if (i != 0) {
      out += ",";
    }
    out += json_string(values[i]);
  }
  out += "]";
  return out;
}

std::optional<std::string> data_object(std::string_view arguments_json) {
  return protocol::extract_object_property(arguments_json, "data");
}

std::optional<std::string> string_arg(std::string_view arguments_json, std::string_view key) {
  if (auto value = protocol::extract_string_property(arguments_json, key)) {
    return value;
  }
  if (auto data = data_object(arguments_json)) {
    return protocol::extract_string_property(*data, key);
  }
  return std::nullopt;
}

std::optional<double> number_arg(std::string_view arguments_json, std::string_view key) {
  if (auto value = protocol::extract_number_property(arguments_json, key)) {
    return value;
  }
  if (auto data = data_object(arguments_json)) {
    return protocol::extract_number_property(*data, key);
  }
  return std::nullopt;
}

bool bool_arg(std::string_view arguments_json, std::string_view key, bool fallback = false) {
  if (auto value = protocol::extract_bool_property(arguments_json, key)) {
    return *value;
  }
  if (auto data = data_object(arguments_json)) {
    if (auto value = protocol::extract_bool_property(*data, key)) {
      return *value;
    }
  }
  return fallback;
}

std::optional<std::string> raw_arg(std::string_view arguments_json, std::string_view key) {
  if (auto value = protocol::extract_object_property(arguments_json, key)) {
    return value;
  }
  if (auto data = data_object(arguments_json)) {
    return protocol::extract_object_property(*data, key);
  }
  return std::nullopt;
}

std::vector<double> number_array_arg(std::string_view arguments_json, std::string_view key) {
  std::vector<double> numbers;
  auto raw = raw_arg(arguments_json, key);
  if (!raw) {
    return numbers;
  }
  for (std::size_t i = 0; i < raw->size();) {
    const char ch = (*raw)[i];
    if (!std::isdigit(static_cast<unsigned char>(ch)) && ch != '-' && ch != '+' && ch != '.') {
      ++i;
      continue;
    }
    std::size_t end = i + 1;
    while (end < raw->size()) {
      const char next = (*raw)[end];
      if (!std::isdigit(static_cast<unsigned char>(next)) && next != '-' && next != '+' &&
          next != '.' && next != 'e' && next != 'E') {
        break;
      }
      ++end;
    }
    try {
      numbers.push_back(std::stod(raw->substr(i, end - i)));
    } catch (...) {
      numbers.clear();
      return numbers;
    }
    i = end;
  }
  return numbers;
}

std::vector<std::string> string_array_arg(std::string_view arguments_json, std::string_view key) {
  std::vector<std::string> values;
  auto raw = raw_arg(arguments_json, key);
  if (!raw) {
    return values;
  }
  bool in_string = false;
  bool escaped = false;
  std::string current;
  for (char ch : *raw) {
    if (!in_string) {
      if (ch == '"') {
        in_string = true;
        current.clear();
      }
      continue;
    }
    if (escaped) {
      escaped = false;
      current += ch;
      continue;
    }
    if (ch == '\\') {
      escaped = true;
      continue;
    }
    if (ch == '"') {
      in_string = false;
      values.push_back(current);
      continue;
    }
    current += ch;
  }
  return values;
}

std::wstring widen_lossy(std::string_view value) {
  if (value.empty()) {
    return {};
  }
  const int required = MultiByteToWideChar(
    CP_UTF8,
    MB_ERR_INVALID_CHARS,
    value.data(),
    static_cast<int>(value.size()),
    nullptr,
    0);
  if (required <= 0) {
    return std::wstring(value.begin(), value.end());
  }
  std::wstring out(static_cast<std::size_t>(required), L'\0');
  MultiByteToWideChar(
    CP_UTF8,
    MB_ERR_INVALID_CHARS,
    value.data(),
    static_cast<int>(value.size()),
    out.data(),
    required);
  return out;
}

std::string upper_copy(std::string value) {
  std::transform(value.begin(), value.end(), value.begin(), [](unsigned char ch) {
    return static_cast<char>(std::toupper(ch));
  });
  return value;
}

core::CommandResult missing_arg(std::string_view name) {
  return core::CommandResult::failure(
    core::ErrorCategory::invalid_arg,
    "Missing required argument: " + std::string(name));
}

class VariantHolder {
 public:
  VariantHolder() { VariantInit(&value); }
  VariantHolder(const VariantHolder&) = delete;
  VariantHolder& operator=(const VariantHolder&) = delete;
  ~VariantHolder() { VariantClear(&value); }

  VARIANT value{};
};

class VariantArgs {
 public:
  VariantArgs() = default;
  VariantArgs(const VariantArgs&) = delete;
  VariantArgs& operator=(const VariantArgs&) = delete;
  ~VariantArgs() {
    for (auto& arg : args_) {
      VariantClear(&arg);
    }
  }

  void add_bstr(std::string_view value) {
    VARIANTARG arg;
    VariantInit(&arg);
    const auto wide = widen_lossy(value);
    arg.vt = VT_BSTR;
    arg.bstrVal = SysAllocStringLen(wide.data(), static_cast<UINT>(wide.size()));
    args_.push_back(arg);
  }

  void add_double(double value) {
    VARIANTARG arg;
    VariantInit(&arg);
    arg.vt = VT_R8;
    arg.dblVal = value;
    args_.push_back(arg);
  }

  void add_i4(long value) {
    VARIANTARG arg;
    VariantInit(&arg);
    arg.vt = VT_I4;
    arg.lVal = value;
    args_.push_back(arg);
  }

  void add_bool(bool value) {
    VARIANTARG arg;
    VariantInit(&arg);
    arg.vt = VT_BOOL;
    arg.boolVal = value ? VARIANT_TRUE : VARIANT_FALSE;
    args_.push_back(arg);
  }

  void add_empty() {
    VARIANTARG arg;
    VariantInit(&arg);
    arg.vt = VT_EMPTY;
    args_.push_back(arg);
  }

  void add_dispatch(IDispatch* dispatch) {
    VARIANTARG arg;
    VariantInit(&arg);
    arg.vt = VT_DISPATCH;
    arg.pdispVal = dispatch;
    if (arg.pdispVal != nullptr) {
      arg.pdispVal->AddRef();
    }
    args_.push_back(arg);
  }

  void add_point(double x, double y, double z = 0.0) {
    add_double_array({x, y, z});
  }

  void add_double_array(const std::vector<double>& values) {
    VARIANTARG arg;
    VariantInit(&arg);
    SAFEARRAY* array = SafeArrayCreateVector(VT_R8, 0, static_cast<ULONG>(values.size()));
    if (array != nullptr) {
      for (LONG i = 0; i < static_cast<LONG>(values.size()); ++i) {
        double value = values[static_cast<std::size_t>(i)];
        SafeArrayPutElement(array, &i, &value);
      }
    }
    arg.vt = VT_ARRAY | VT_R8;
    arg.parray = array;
    args_.push_back(arg);
  }

  void add_dispatch_array(const std::vector<IDispatch*>& values) {
    VARIANTARG arg;
    VariantInit(&arg);
    SAFEARRAY* array = SafeArrayCreateVector(VT_DISPATCH, 0, static_cast<ULONG>(values.size()));
    if (array != nullptr) {
      for (LONG i = 0; i < static_cast<LONG>(values.size()); ++i) {
        IDispatch* value = values[static_cast<std::size_t>(i)];
        SafeArrayPutElement(array, &i, value);
      }
    }
    arg.vt = VT_ARRAY | VT_DISPATCH;
    arg.parray = array;
    args_.push_back(arg);
  }

  [[nodiscard]] VARIANTARG* data() { return args_.empty() ? nullptr : args_.data(); }
  [[nodiscard]] UINT size() const { return static_cast<UINT>(args_.size()); }

 private:
  std::vector<VARIANTARG> args_;
};

bool should_retry(HRESULT hr) {
  return hr == RPC_E_CALL_REJECTED || hr == RPC_E_SERVERCALL_RETRYLATER;
}

bool get_dispid(IDispatch* dispatch, const wchar_t* name, DISPID& dispid, std::string& error) {
  LPOLESTR names[1] = {const_cast<LPOLESTR>(name)};
  const HRESULT hr = dispatch->GetIDsOfNames(IID_NULL, names, 1, LOCALE_USER_DEFAULT, &dispid);
  if (FAILED(hr)) {
    error = "GetIDsOfNames failed: " + hresult_hex(hr);
    return false;
  }
  return true;
}

std::string excepinfo_message(const EXCEPINFO& info) {
  std::string message;
  if (info.bstrSource != nullptr) {
    message += narrow_lossy(info.bstrSource);
  }
  if (info.bstrDescription != nullptr) {
    if (!message.empty()) {
      message += ": ";
    }
    message += narrow_lossy(info.bstrDescription);
  }
  return message;
}

void clear_excepinfo(EXCEPINFO& info) {
  SysFreeString(info.bstrSource);
  SysFreeString(info.bstrDescription);
  SysFreeString(info.bstrHelpFile);
  info.bstrSource = nullptr;
  info.bstrDescription = nullptr;
  info.bstrHelpFile = nullptr;
}

bool invoke_raw(
    IDispatch* dispatch,
    const wchar_t* name,
    WORD flags,
    VariantArgs& args,
    VARIANT* result,
    std::string& error,
    DISPID* named_args = nullptr,
    UINT named_arg_count = 0) {
  if (dispatch == nullptr) {
    error = "IDispatch pointer is null";
    return false;
  }
  DISPID dispid = 0;
  if (!get_dispid(dispatch, name, dispid, error)) {
    return false;
  }
  DISPPARAMS params{};
  params.rgvarg = args.data();
  params.cArgs = args.size();
  params.rgdispidNamedArgs = named_args;
  params.cNamedArgs = named_arg_count;

  EXCEPINFO excep{};
  UINT arg_error = 0;
  HRESULT hr = E_FAIL;
  for (int attempt = 0; attempt < 4; ++attempt) {
    hr = dispatch->Invoke(
      dispid,
      IID_NULL,
      LOCALE_USER_DEFAULT,
      flags,
      &params,
      result,
      &excep,
      &arg_error);
    if (!should_retry(hr)) {
      break;
    }
    clear_excepinfo(excep);
    std::this_thread::sleep_for(std::chrono::milliseconds(50 * (attempt + 1)));
  }
  if (FAILED(hr)) {
    error = "Invoke failed: " + hresult_hex(hr);
    const auto details = excepinfo_message(excep);
    if (!details.empty()) {
      error += " (" + details + ")";
    }
    if (arg_error != 0) {
      error += " argument=" + std::to_string(arg_error);
    }
    clear_excepinfo(excep);
    return false;
  }
  clear_excepinfo(excep);
  return true;
}

bool property_get(IDispatch* dispatch, const wchar_t* name, VariantHolder& out, std::string& error) {
  VariantArgs args;
  return invoke_raw(dispatch, name, DISPATCH_PROPERTYGET, args, &out.value, error);
}

bool property_put(IDispatch* dispatch, const wchar_t* name, VariantArgs& args, std::string& error) {
  DISPID property_put_id = DISPID_PROPERTYPUT;
  VariantHolder ignored;
  return invoke_raw(
    dispatch,
    name,
    DISPATCH_PROPERTYPUT,
    args,
    &ignored.value,
    error,
    &property_put_id,
    1);
}

bool method_call(IDispatch* dispatch, const wchar_t* name, VariantArgs& args, VariantHolder& out, std::string& error) {
  return invoke_raw(dispatch, name, DISPATCH_METHOD, args, &out.value, error);
}

std::string get_string_prop(IDispatch* dispatch, const wchar_t* name);

ComPtr<IDispatch> dispatch_from_variant(const VARIANT& value) {
  if (value.vt == VT_DISPATCH && value.pdispVal != nullptr) {
    value.pdispVal->AddRef();
    return ComPtr<IDispatch>(value.pdispVal);
  }
  if (value.vt == VT_UNKNOWN && value.punkVal != nullptr) {
    ComPtr<IDispatch> dispatch;
    value.punkVal->QueryInterface(IID_IDispatch, reinterpret_cast<void**>(dispatch.put()));
    return dispatch;
  }
  return ComPtr<IDispatch>();
}

std::vector<std::string> handles_from_dispatch_array(const VARIANT& value) {
  std::vector<std::string> handles;
  if ((value.vt & VT_ARRAY) == 0 || value.parray == nullptr) {
    auto single = dispatch_from_variant(value);
    if (single) {
      handles.push_back(get_string_prop(single.get(), L"Handle"));
    }
    return handles;
  }

  LONG lower = 0;
  LONG upper = -1;
  if (FAILED(SafeArrayGetLBound(value.parray, 1, &lower)) ||
      FAILED(SafeArrayGetUBound(value.parray, 1, &upper))) {
    return handles;
  }

  for (LONG i = lower; i <= upper; ++i) {
    if ((value.vt & VT_DISPATCH) == VT_DISPATCH) {
      IDispatch* dispatch = nullptr;
      if (SUCCEEDED(SafeArrayGetElement(value.parray, &i, &dispatch)) && dispatch != nullptr) {
        ComPtr<IDispatch> item(dispatch);
        handles.push_back(get_string_prop(item.get(), L"Handle"));
      }
    } else if ((value.vt & VT_VARIANT) == VT_VARIANT) {
      VariantHolder item;
      if (SUCCEEDED(SafeArrayGetElement(value.parray, &i, &item.value))) {
        auto dispatch = dispatch_from_variant(item.value);
        if (dispatch) {
          handles.push_back(get_string_prop(dispatch.get(), L"Handle"));
        }
      }
    }
  }
  return handles;
}

std::optional<std::string> string_from_variant(const VARIANT& value) {
  if (value.vt == VT_BSTR) {
    return narrow_lossy(value.bstrVal);
  }
  if (value.vt == VT_EMPTY || value.vt == VT_NULL) {
    return std::nullopt;
  }
  VariantHolder converted;
  if (SUCCEEDED(VariantChangeType(&converted.value, const_cast<VARIANT*>(&value), 0, VT_BSTR)) &&
      converted.value.vt == VT_BSTR) {
    return narrow_lossy(converted.value.bstrVal);
  }
  return std::nullopt;
}

std::optional<long> long_from_variant(const VARIANT& value) {
  if (value.vt == VT_I4) {
    return value.lVal;
  }
  if (value.vt == VT_I2) {
    return value.iVal;
  }
  VariantHolder converted;
  if (SUCCEEDED(VariantChangeType(&converted.value, const_cast<VARIANT*>(&value), 0, VT_I4)) &&
      converted.value.vt == VT_I4) {
    return converted.value.lVal;
  }
  return std::nullopt;
}

std::optional<bool> bool_from_variant(const VARIANT& value) {
  if (value.vt == VT_BOOL) {
    return value.boolVal == VARIANT_TRUE;
  }
  return std::nullopt;
}

std::vector<double> double_array_from_variant(const VARIANT& value) {
  std::vector<double> numbers;
  if ((value.vt & VT_ARRAY) == 0 || value.parray == nullptr) {
    return numbers;
  }
  LONG lower = 0;
  LONG upper = -1;
  if (FAILED(SafeArrayGetLBound(value.parray, 1, &lower)) ||
      FAILED(SafeArrayGetUBound(value.parray, 1, &upper))) {
    return numbers;
  }
  for (LONG i = lower; i <= upper; ++i) {
    if ((value.vt & VT_R8) == VT_R8) {
      double item = 0.0;
      if (SUCCEEDED(SafeArrayGetElement(value.parray, &i, &item))) {
        numbers.push_back(item);
      }
    } else if ((value.vt & VT_VARIANT) == VT_VARIANT) {
      VariantHolder item;
      if (SUCCEEDED(SafeArrayGetElement(value.parray, &i, &item.value))) {
        VariantHolder converted;
        if (SUCCEEDED(VariantChangeType(&converted.value, &item.value, 0, VT_R8))) {
          numbers.push_back(converted.value.dblVal);
        }
      }
    }
  }
  return numbers;
}

std::string get_string_prop(IDispatch* dispatch, const wchar_t* name) {
  LPOLESTR prop = const_cast<LPOLESTR>(name);
  return get_string_property(dispatch, prop);
}

std::vector<double> get_double_array_prop(IDispatch* dispatch, const wchar_t* name) {
  VariantHolder value;
  std::string error;
  if (!property_get(dispatch, name, value, error)) {
    return {};
  }
  return double_array_from_variant(value.value);
}

std::string dxf_type_from_object_name(std::string object_name) {
  if (object_name.rfind("AcDb", 0) == 0) {
    object_name.erase(0, 4);
  }
  return upper_copy(std::move(object_name));
}

long get_long_prop(IDispatch* dispatch, const wchar_t* name, long fallback = 0) {
  VariantHolder value;
  std::string error;
  if (!property_get(dispatch, name, value, error)) {
    return fallback;
  }
  return long_from_variant(value.value).value_or(fallback);
}

double get_double_prop(IDispatch* dispatch, const wchar_t* name, double fallback = 0.0) {
  VariantHolder value;
  std::string error;
  if (!property_get(dispatch, name, value, error)) {
    return fallback;
  }
  if (value.value.vt == VT_R8) {
    return value.value.dblVal;
  }
  VariantHolder converted;
  if (SUCCEEDED(VariantChangeType(&converted.value, &value.value, 0, VT_R8)) &&
      converted.value.vt == VT_R8) {
    return converted.value.dblVal;
  }
  return fallback;
}

bool get_bool_prop(IDispatch* dispatch, const wchar_t* name, bool fallback = false) {
  VariantHolder value;
  std::string error;
  if (!property_get(dispatch, name, value, error)) {
    return fallback;
  }
  return bool_from_variant(value.value).value_or(fallback);
}

core::CommandResult com_failure(std::string_view context, const std::string& error) {
  return core::CommandResult::failure(
    core::ErrorCategory::internal_error,
    std::string(context) + ": " + error);
}

core::CommandResult required_numeric(std::string_view arguments_json, std::string_view key, double& out) {
  auto value = number_arg(arguments_json, key);
  if (!value) {
    return missing_arg(key);
  }
  out = *value;
  return core::CommandResult::success();
}

class AutoCadComSession {
 public:
  bool attach() {
    if (FAILED(apartment_.result())) {
      error_ = "CoInitializeEx failed: " + hresult_hex(apartment_.result());
      return false;
    }
    CLSID clsid{};
    HRESULT hr = CLSIDFromProgID(L"AutoCAD.Application", &clsid);
    if (FAILED(hr)) {
      error_ = "AutoCAD.Application ProgID is not registered: " + hresult_hex(hr);
      return false;
    }
    ComPtr<IUnknown> unknown;
    hr = GetActiveObject(clsid, nullptr, unknown.put());
    if (FAILED(hr)) {
      error_ = "No running AutoCAD COM application found: " + hresult_hex(hr);
      return false;
    }
    hr = unknown.get()->QueryInterface(IID_IDispatch, reinterpret_cast<void**>(app_.put()));
    if (FAILED(hr) || !app_) {
      error_ = "Running AutoCAD object does not expose IDispatch: " + hresult_hex(hr);
      return false;
    }
    return true;
  }

  [[nodiscard]] IDispatch* app() const { return app_.get(); }
  [[nodiscard]] const std::string& error() const { return error_; }

  ComPtr<IDispatch> active_document(std::string& error) const {
    VariantHolder result;
    if (!property_get(app_.get(), L"ActiveDocument", result, error)) {
      return {};
    }
    return dispatch_from_variant(result.value);
  }

  ComPtr<IDispatch> documents(std::string& error) const {
    VariantHolder result;
    if (!property_get(app_.get(), L"Documents", result, error)) {
      return {};
    }
    return dispatch_from_variant(result.value);
  }

  ComPtr<IDispatch> model_space(std::string& error) const {
    auto doc = active_document(error);
    if (!doc) {
      return {};
    }
    VariantHolder result;
    if (!property_get(doc.get(), L"ModelSpace", result, error)) {
      return {};
    }
    return dispatch_from_variant(result.value);
  }

 private:
  ComApartment apartment_;
  ComPtr<IDispatch> app_;
  std::string error_;
};

ComPtr<IDispatch> named_collection(IDispatch* doc, const wchar_t* name, std::string& error) {
  VariantHolder result;
  if (!property_get(doc, name, result, error)) {
    return {};
  }
  return dispatch_from_variant(result.value);
}

ComPtr<IDispatch> collection_item(IDispatch* collection, long index, std::string& error) {
  VariantArgs args;
  args.add_i4(index);
  VariantHolder result;
  if (!method_call(collection, L"Item", args, result, error)) {
    return {};
  }
  return dispatch_from_variant(result.value);
}

ComPtr<IDispatch> collection_item(IDispatch* collection, std::string_view name, std::string& error) {
  VariantArgs args;
  args.add_bstr(name);
  VariantHolder result;
  if (!method_call(collection, L"Item", args, result, error)) {
    return {};
  }
  return dispatch_from_variant(result.value);
}

core::CommandResult attach_failure(const AutoCadComSession& session) {
  return core::CommandResult::failure(core::ErrorCategory::backend_unavailable, session.error());
}

core::CommandResult entity_payload(IDispatch* entity, std::string_view type_hint = "") {
  const std::string handle = get_string_prop(entity, L"Handle");
  const std::string object_name = get_string_prop(entity, L"ObjectName");
  const std::string layer = get_string_prop(entity, L"Layer");
  return core::CommandResult::success(
    std::string("{\"handle\":") + json_string(handle) +
    ",\"entity_id\":" + json_string(handle) +
    ",\"type\":" + json_string(type_hint.empty() ? object_name : type_hint) +
    ",\"object_name\":" + json_string(object_name) +
    ",\"layer\":" + json_string(layer) +
    ",\"backend\":\"com\"}");
}

std::string layers_json(IDispatch* layers) {
  const long count = get_long_prop(layers, L"Count", 0);
  std::string items = "[";
  bool first = true;
  std::string ignored;
  for (long i = 0; i < count; ++i) {
    auto layer = collection_item(layers, i, ignored);
    if (!layer) {
      ignored.clear();
      layer = collection_item(layers, i + 1, ignored);
    }
    if (!layer) {
      continue;
    }
    if (!first) {
      items += ",";
    }
    first = false;
    items += "{\"name\":" + json_string(get_string_prop(layer.get(), L"Name")) +
      ",\"color\":" + std::to_string(get_long_prop(layer.get(), L"Color", 7)) +
      ",\"linetype\":" + json_string(get_string_prop(layer.get(), L"Linetype")) +
      ",\"frozen\":" + (get_bool_prop(layer.get(), L"Freeze") ? "true" : "false") +
      ",\"locked\":" + (get_bool_prop(layer.get(), L"Lock") ? "true" : "false") +
      "}";
  }
  items += "]";
  return items;
}

std::string collection_names_json(IDispatch* collection, std::string_view field_name) {
  const long count = get_long_prop(collection, L"Count", 0);
  std::string items = "[";
  bool first = true;
  std::string ignored;
  for (long i = 0; i < count; ++i) {
    auto item = collection_item(collection, i, ignored);
    if (!item) {
      ignored.clear();
      item = collection_item(collection, i + 1, ignored);
    }
    if (!item) {
      continue;
    }
    if (!first) {
      items += ",";
    }
    first = false;
    items += json_string(get_string_prop(item.get(), L"Name"));
  }
  items += "]";
  return "{\"" + std::string(field_name) + "\":" + items + ",\"count\":" + std::to_string(count) + "}";
}

core::CommandResult layer_payload(IDispatch* layer, std::string_view action) {
  return core::CommandResult::success(
    std::string("{\"action\":") + json_string(action) +
    ",\"name\":" + json_string(get_string_prop(layer, L"Name")) +
    ",\"color\":" + std::to_string(get_long_prop(layer, L"Color", 7)) +
    ",\"linetype\":" + json_string(get_string_prop(layer, L"Linetype")) +
    ",\"frozen\":" + (get_bool_prop(layer, L"Freeze") ? "true" : "false") +
    ",\"locked\":" + (get_bool_prop(layer, L"Lock") ? "true" : "false") +
    ",\"backend\":\"com\"}");
}

core::CommandResult send_document_command(AutoCadComSession& session, std::string_view command) {
  std::string error;
  auto doc = session.active_document(error);
  if (!doc) {
    return com_failure("ActiveDocument", error);
  }
  VariantArgs args;
  args.add_bstr(command);
  VariantHolder ignored;
  if (!method_call(doc.get(), L"SendCommand", args, ignored, error)) {
    return com_failure("ActiveDocument.SendCommand", error);
  }
  return core::CommandResult::success("{\"queued\":true,\"command\":" + json_string(command) + "}");
}

core::CommandResult drawing_info(AutoCadComSession& session) {
  std::string error;
  auto doc = session.active_document(error);
  if (!doc) {
    return com_failure("ActiveDocument", error);
  }
  auto model = session.model_space(error);
  auto layers = named_collection(doc.get(), L"Layers", error);
  auto blocks = named_collection(doc.get(), L"Blocks", error);

  std::string current_layer;
  VariantHolder active_layer;
  if (property_get(doc.get(), L"ActiveLayer", active_layer, error)) {
    auto layer_dispatch = dispatch_from_variant(active_layer.value);
    if (layer_dispatch) {
      current_layer = get_string_prop(layer_dispatch.get(), L"Name");
    }
  }

  return core::CommandResult::success(
    std::string("{\"name\":") + json_string(get_string_prop(doc.get(), L"Name")) +
    ",\"path\":" + json_string(get_string_prop(doc.get(), L"FullName")) +
    ",\"application\":" + json_string(get_string_prop(session.app(), L"Name")) +
    ",\"application_version\":" + json_string(get_string_prop(session.app(), L"Version")) +
    ",\"current_layer\":" + json_string(current_layer) +
    ",\"entity_count\":" + std::to_string(model ? get_long_prop(model.get(), L"Count", 0) : 0) +
    ",\"layer_count\":" + std::to_string(layers ? get_long_prop(layers.get(), L"Count", 0) : 0) +
    ",\"block_count\":" + std::to_string(blocks ? get_long_prop(blocks.get(), L"Count", 0) : 0) +
    ",\"layers\":" + (layers ? layers_json(layers.get()) : "[]") +
    ",\"backend\":\"com\"}");
}

core::CommandResult drawing_create_or_open(AutoCadComSession& session, std::string_view operation, std::string_view arguments_json) {
  std::string error;
  auto docs = session.documents(error);
  if (!docs) {
    return com_failure("Application.Documents", error);
  }
  VariantArgs args;
  if (operation == "open") {
    auto path = string_arg(arguments_json, "path");
    if (!path || path->empty()) {
      return missing_arg("path");
    }
    args.add_bstr(*path);
  } else if (auto template_path = string_arg(arguments_json, "template"); template_path && !template_path->empty()) {
    args.add_bstr(*template_path);
  }
  VariantHolder result;
  if (!method_call(docs.get(), operation == "open" ? L"Open" : L"Add", args, result, error)) {
    return com_failure(operation == "open" ? "Documents.Open" : "Documents.Add", error);
  }
  auto doc = dispatch_from_variant(result.value);
  if (!doc) {
    return core::CommandResult::success("{\"opened\":true,\"backend\":\"com\"}");
  }
  return core::CommandResult::success(
    std::string("{\"opened\":true,\"name\":") + json_string(get_string_prop(doc.get(), L"Name")) +
    ",\"path\":" + json_string(get_string_prop(doc.get(), L"FullName")) +
    ",\"backend\":\"com\"}");
}

int save_as_code(std::string_view operation, std::string version) {
  version = upper_copy(std::move(version));
  if (operation == "save_as_dxf") {
    if (version.find("2018") != std::string::npos || version.find("AC1032") != std::string::npos) return 65;
    if (version.find("2013") != std::string::npos || version.find("AC1027") != std::string::npos) return 61;
    if (version.find("2010") != std::string::npos || version.find("AC1024") != std::string::npos) return 49;
    if (version.find("2007") != std::string::npos || version.find("AC1021") != std::string::npos) return 37;
    return 65;
  }
  if (version.find("2018") != std::string::npos || version.find("AC1032") != std::string::npos) return 64;
  if (version.find("2013") != std::string::npos || version.find("AC1027") != std::string::npos) return 60;
  if (version.find("2010") != std::string::npos || version.find("AC1024") != std::string::npos) return 48;
  if (version.find("2007") != std::string::npos || version.find("AC1021") != std::string::npos) return 36;
  if (version.find("2004") != std::string::npos || version.find("AC1018") != std::string::npos) return 24;
  if (version.find("2000") != std::string::npos || version.find("AC1015") != std::string::npos) return 12;
  if (version.find("R14") != std::string::npos || version.find("AC1014") != std::string::npos) return 8;
  return 64;
}

core::CommandResult drawing_save(AutoCadComSession& session, std::string_view operation, std::string_view arguments_json) {
  std::string error;
  auto doc = session.active_document(error);
  if (!doc) {
    return com_failure("ActiveDocument", error);
  }

  VariantArgs args;
  const auto path = string_arg(arguments_json, "path");
  const bool save_as = operation == "save_as_dxf" || operation == "save_as_dwg" || path.has_value();
  if (save_as) {
    if (!path || path->empty()) {
      return missing_arg("path");
    }
    if (operation == "save_as_dxf" || operation == "save_as_dwg") {
      args.add_i4(save_as_code(operation, string_arg(arguments_json, "version").value_or("2018")));
    }
    args.add_bstr(*path);
  }

  VariantHolder ignored;
  if (!method_call(doc.get(), save_as ? L"SaveAs" : L"Save", args, ignored, error)) {
    return com_failure(save_as ? "Document.SaveAs" : "Document.Save", error);
  }
  return core::CommandResult::success(
    std::string("{\"saved\":true,\"path\":") +
    json_string(path.value_or(get_string_prop(doc.get(), L"FullName"))) +
    ",\"backend\":\"com\"}");
}

core::CommandResult drawing_variables(AutoCadComSession& session) {
  std::string error;
  auto doc = session.active_document(error);
  if (!doc) {
    return com_failure("ActiveDocument", error);
  }
  auto getvar = [&](std::string_view name) -> std::string {
    VariantArgs args;
    args.add_bstr(name);
    VariantHolder result;
    std::string local_error;
    if (!method_call(doc.get(), L"GetVariable", args, result, local_error)) {
      return "null";
    }
    if (auto text = string_from_variant(result.value)) {
      return json_string(*text);
    }
    if (auto number = long_from_variant(result.value)) {
      return std::to_string(*number);
    }
    return "null";
  };
  return core::CommandResult::success(
    std::string("{\"$ACADVER\":") + getvar("ACADVER") +
    ",\"$CLAYER\":" + getvar("CLAYER") +
    ",\"$DWGCODEPAGE\":" + getvar("DWGCODEPAGE") +
    ",\"backend\":\"com\"}");
}

std::string layouts_json(IDispatch* layouts) {
  const long count = get_long_prop(layouts, L"Count", 0);
  std::string items = "[";
  bool first = true;
  std::string ignored;
  for (long i = 0; i < count; ++i) {
    auto layout = collection_item(layouts, i, ignored);
    if (!layout) {
      ignored.clear();
      layout = collection_item(layouts, i + 1, ignored);
    }
    if (!layout) {
      continue;
    }
    if (!first) {
      items += ",";
    }
    first = false;
    items += "{\"name\":" + json_string(get_string_prop(layout.get(), L"Name")) +
      ",\"tab_order\":" + std::to_string(get_long_prop(layout.get(), L"TabOrder", 0)) +
      "}";
  }
  items += "]";
  return items;
}

core::CommandResult drawing_plot_pdf(AutoCadComSession& session, std::string_view arguments_json) {
  auto path = string_arg(arguments_json, "path");
  if (!path || path->empty()) {
    path = string_arg(arguments_json, "output");
  }
  if (!path || path->empty()) {
    return missing_arg("path");
  }

  std::string error;
  auto doc = session.active_document(error);
  if (!doc) {
    return com_failure("ActiveDocument", error);
  }
  VariantHolder plot_value;
  if (!property_get(doc.get(), L"Plot", plot_value, error)) {
    return com_failure("Document.Plot", error);
  }
  auto plot = dispatch_from_variant(plot_value.value);
  if (!plot) {
    return core::CommandResult::failure(core::ErrorCategory::internal_error, "Document.Plot did not return IDispatch");
  }

  VariantArgs args;
  if (auto plotter = string_arg(arguments_json, "plotter"); plotter && !plotter->empty()) {
    args.add_bstr(*plotter);
  }
  args.add_bstr(*path);
  VariantHolder result;
  if (!method_call(plot.get(), L"PlotToFile", args, result, error)) {
    return com_failure("Plot.PlotToFile", error);
  }
  const bool plotted = bool_from_variant(result.value).value_or(true);
  return core::CommandResult::success(
    std::string("{\"plotted\":") + (plotted ? "true" : "false") +
    ",\"path\":" + json_string(*path) +
    ",\"backend\":\"com\"}");
}

core::CommandResult drawing_layout_operation(AutoCadComSession& session, std::string_view operation, std::string_view arguments_json) {
  std::string error;
  auto doc = session.active_document(error);
  if (!doc) {
    return com_failure("ActiveDocument", error);
  }
  auto layouts = named_collection(doc.get(), L"Layouts", error);
  if (!layouts) {
    return com_failure("Document.Layouts", error);
  }

  if (operation == "list_layouts") {
    std::string active;
    VariantHolder active_value;
    if (property_get(doc.get(), L"ActiveLayout", active_value, error)) {
      auto active_layout = dispatch_from_variant(active_value.value);
      if (active_layout) {
        active = get_string_prop(active_layout.get(), L"Name");
      }
    }
    return core::CommandResult::success(
      "{\"layouts\":" + layouts_json(layouts.get()) +
      ",\"active_layout\":" + json_string(active) +
      ",\"backend\":\"com\"}");
  }

  auto name = string_arg(arguments_json, "name");
  if (!name || name->empty()) {
    return missing_arg("name");
  }

  ComPtr<IDispatch> layout;
  if (operation == "create_layout") {
    VariantArgs args;
    args.add_bstr(*name);
    VariantHolder result;
    if (!method_call(layouts.get(), L"Add", args, result, error)) {
      return com_failure("Layouts.Add", error);
    }
    layout = dispatch_from_variant(result.value);
  } else {
    layout = collection_item(layouts.get(), *name, error);
    if (!layout) {
      return core::CommandResult::failure(core::ErrorCategory::not_found, error);
    }
  }

  if (operation == "set_active_layout" || operation == "create_layout") {
    VariantArgs args;
    args.add_dispatch(layout.get());
    if (!property_put(doc.get(), L"ActiveLayout", args, error)) {
      return com_failure("Document.ActiveLayout", error);
    }
    return core::CommandResult::success(
      std::string("{\"active_layout\":") + json_string(get_string_prop(layout.get(), L"Name")) +
      ",\"backend\":\"com\"}");
  }

  if (operation == "create_viewport") {
    VariantHolder block_value;
    if (!property_get(layout.get(), L"Block", block_value, error)) {
      return com_failure("Layout.Block", error);
    }
    auto block = dispatch_from_variant(block_value.value);
    if (!block) {
      return core::CommandResult::failure(core::ErrorCategory::internal_error, "Layout.Block did not return IDispatch");
    }
    const double cx = number_arg(arguments_json, "cx").value_or(number_arg(arguments_json, "x").value_or(5.0));
    const double cy = number_arg(arguments_json, "cy").value_or(number_arg(arguments_json, "y").value_or(5.0));
    const double width = number_arg(arguments_json, "width").value_or(8.0);
    const double height = number_arg(arguments_json, "height").value_or(6.0);
    VariantArgs args;
    args.add_double(height);
    args.add_double(width);
    args.add_point(cx, cy);
    VariantHolder result;
    if (!method_call(block.get(), L"AddPViewport", args, result, error)) {
      return com_failure("Layout.Block.AddPViewport", error);
    }
    auto viewport = dispatch_from_variant(result.value);
    if (!viewport) {
      return core::CommandResult::success("{\"created\":true,\"type\":\"PVIEWPORT\",\"backend\":\"com\"}");
    }
    return entity_payload(viewport.get(), "PVIEWPORT");
  }

  return core::not_supported("COM layout operation is not implemented");
}

core::CommandResult create_entity(AutoCadComSession& session, std::string_view operation, std::string_view arguments_json) {
  constexpr double kPi = 3.14159265358979323846;
  std::string error;
  auto model = session.model_space(error);
  if (!model) {
    return com_failure("Document.ModelSpace", error);
  }

  VariantArgs args;
  const wchar_t* method = nullptr;
  const char* type_hint = "";

  if (operation == "create_line") {
    double x1{}, y1{}, x2{}, y2{};
    if (auto missing = required_numeric(arguments_json, "x1", x1); !missing.ok) return missing;
    if (auto missing = required_numeric(arguments_json, "y1", y1); !missing.ok) return missing;
    if (auto missing = required_numeric(arguments_json, "x2", x2); !missing.ok) return missing;
    if (auto missing = required_numeric(arguments_json, "y2", y2); !missing.ok) return missing;
    method = L"AddLine";
    type_hint = "LINE";
    args.add_point(x2, y2);
    args.add_point(x1, y1);
  } else if (operation == "create_circle") {
    double cx{}, cy{}, radius{};
    if (auto missing = required_numeric(arguments_json, "cx", cx); !missing.ok) return missing;
    if (auto missing = required_numeric(arguments_json, "cy", cy); !missing.ok) return missing;
    if (auto missing = required_numeric(arguments_json, "radius", radius); !missing.ok) return missing;
    method = L"AddCircle";
    type_hint = "CIRCLE";
    args.add_double(radius);
    args.add_point(cx, cy);
  } else if (operation == "create_text" || operation == "create_mtext") {
    double x{}, y{};
    auto text = string_arg(arguments_json, "text");
    if (auto missing = required_numeric(arguments_json, "x", x); !missing.ok) return missing;
    if (auto missing = required_numeric(arguments_json, "y", y); !missing.ok) return missing;
    if (!text) return missing_arg("text");
    if (operation == "create_mtext") {
      method = L"AddMText";
      type_hint = "MTEXT";
      args.add_bstr(*text);
      args.add_double(number_arg(arguments_json, "width").value_or(10.0));
      args.add_point(x, y);
    } else {
      method = L"AddText";
      type_hint = "TEXT";
      args.add_double(number_arg(arguments_json, "height").value_or(2.5));
      args.add_point(x, y);
      args.add_bstr(*text);
    }
  } else if (operation == "create_polyline" || operation == "create_rectangle") {
    std::vector<double> coords;
    if (operation == "create_rectangle") {
      double x1{}, y1{}, x2{}, y2{};
      if (auto missing = required_numeric(arguments_json, "x1", x1); !missing.ok) return missing;
      if (auto missing = required_numeric(arguments_json, "y1", y1); !missing.ok) return missing;
      if (auto missing = required_numeric(arguments_json, "x2", x2); !missing.ok) return missing;
      if (auto missing = required_numeric(arguments_json, "y2", y2); !missing.ok) return missing;
      coords = {x1, y1, x2, y1, x2, y2, x1, y2};
    } else {
      coords = number_array_arg(arguments_json, "points");
      if (coords.size() < 4 || coords.size() % 2 != 0) {
        return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "points must be an array of [x,y] pairs");
      }
    }
    method = L"AddLightWeightPolyline";
    type_hint = "LWPOLYLINE";
    args.add_double_array(coords);
  } else if (operation == "create_arc") {
    double cx{}, cy{}, radius{}, start_angle{}, end_angle{};
    if (auto missing = required_numeric(arguments_json, "cx", cx); !missing.ok) return missing;
    if (auto missing = required_numeric(arguments_json, "cy", cy); !missing.ok) return missing;
    if (auto missing = required_numeric(arguments_json, "radius", radius); !missing.ok) return missing;
    if (auto missing = required_numeric(arguments_json, "start_angle", start_angle); !missing.ok) return missing;
    if (auto missing = required_numeric(arguments_json, "end_angle", end_angle); !missing.ok) return missing;
    method = L"AddArc";
    type_hint = "ARC";
    args.add_double(end_angle * kPi / 180.0);
    args.add_double(start_angle * kPi / 180.0);
    args.add_double(radius);
    args.add_point(cx, cy);
  } else if (operation == "create_ellipse") {
    double cx{}, cy{}, major_x{}, major_y{}, ratio{};
    if (auto missing = required_numeric(arguments_json, "cx", cx); !missing.ok) return missing;
    if (auto missing = required_numeric(arguments_json, "cy", cy); !missing.ok) return missing;
    if (auto missing = required_numeric(arguments_json, "major_x", major_x); !missing.ok) return missing;
    if (auto missing = required_numeric(arguments_json, "major_y", major_y); !missing.ok) return missing;
    if (auto missing = required_numeric(arguments_json, "ratio", ratio); !missing.ok) return missing;
    method = L"AddEllipse";
    type_hint = "ELLIPSE";
    args.add_double(ratio);
    args.add_point(major_x, major_y);
    args.add_point(cx, cy);
  } else {
    return core::not_supported("COM entity creation operation is not implemented");
  }

  VariantHolder result;
  if (!method_call(model.get(), method, args, result, error)) {
    return com_failure("ModelSpace entity creation", error);
  }
  auto entity = dispatch_from_variant(result.value);
  if (!entity) {
    return core::CommandResult::success("{\"created\":true,\"backend\":\"com\"}");
  }
  if (auto layer = string_arg(arguments_json, "layer"); layer && !layer->empty()) {
    VariantArgs put_args;
    put_args.add_bstr(*layer);
    std::string layer_error;
    (void)property_put(entity.get(), L"Layer", put_args, layer_error);
  }
  if (operation == "create_text" || operation == "create_mtext") {
    if (auto rotation = number_arg(arguments_json, "rotation")) {
      VariantArgs put_args;
      put_args.add_double(*rotation * kPi / 180.0);
      std::string rotation_error;
      (void)property_put(entity.get(), L"Rotation", put_args, rotation_error);
    }
  }
  if ((operation == "create_polyline" || operation == "create_rectangle") &&
      bool_arg(arguments_json, "closed", operation == "create_rectangle")) {
    VariantArgs put_args;
    put_args.add_bool(true);
    std::string closed_error;
    (void)property_put(entity.get(), L"Closed", put_args, closed_error);
  }
  return entity_payload(entity.get(), type_hint);
}

ComPtr<IDispatch> object_by_handle_value(AutoCadComSession& session, std::string_view handle, std::string& error) {
  if (handle.empty()) {
    error = "entity_id is required";
    return {};
  }
  auto doc = session.active_document(error);
  if (!doc) {
    return {};
  }
  VariantArgs args;
  args.add_bstr(handle);
  VariantHolder result;
  if (!method_call(doc.get(), L"HandleToObject", args, result, error)) {
    return {};
  }
  return dispatch_from_variant(result.value);
}

ComPtr<IDispatch> object_by_handle(AutoCadComSession& session, std::string_view arguments_json, std::string& error) {
  auto handle = string_arg(arguments_json, "entity_id");
  if (!handle) {
    handle = string_arg(arguments_json, "handle");
  }
  if (!handle || handle->empty()) {
    error = "entity_id is required";
    return {};
  }
  return object_by_handle_value(session, *handle, error);
}

core::CommandResult entity_advanced(AutoCadComSession& session, std::string_view operation, std::string_view arguments_json) {
  constexpr double kPi = 3.14159265358979323846;
  std::string error;
  auto model = session.model_space(error);
  if (!model) {
    return com_failure("Document.ModelSpace", error);
  }

  if (operation == "create_hatch") {
    auto target = object_by_handle(session, arguments_json, error);
    if (!target) {
      return core::CommandResult::failure(
        error == "entity_id is required" ? core::ErrorCategory::invalid_arg : core::ErrorCategory::not_found,
        error);
    }
    VariantArgs args;
    args.add_bool(true);
    args.add_bstr(string_arg(arguments_json, "pattern").value_or("ANSI31"));
    args.add_i4(0);
    VariantHolder result;
    if (!method_call(model.get(), L"AddHatch", args, result, error)) {
      return com_failure("ModelSpace.AddHatch", error);
    }
    auto hatch = dispatch_from_variant(result.value);
    if (!hatch) {
      return core::CommandResult::failure(core::ErrorCategory::internal_error, "AddHatch did not return IDispatch");
    }
    VariantArgs loop_args;
    loop_args.add_dispatch_array({target.get()});
    VariantHolder ignored;
    if (!method_call(hatch.get(), L"AppendOuterLoop", loop_args, ignored, error)) {
      return com_failure("Hatch.AppendOuterLoop", error);
    }
    VariantArgs eval_args;
    if (!method_call(hatch.get(), L"Evaluate", eval_args, ignored, error)) {
      return com_failure("Hatch.Evaluate", error);
    }
    return entity_payload(hatch.get(), "HATCH");
  }

  if (operation == "array") {
    auto entity = object_by_handle(session, arguments_json, error);
    if (!entity) {
      return core::CommandResult::failure(
        error == "entity_id is required" ? core::ErrorCategory::invalid_arg : core::ErrorCategory::not_found,
        error);
    }
    VariantArgs args;
    args.add_double(0.0);
    args.add_double(number_arg(arguments_json, "col_dist").value_or(0.0));
    args.add_double(number_arg(arguments_json, "row_dist").value_or(0.0));
    args.add_i4(1);
    args.add_i4(static_cast<long>(number_arg(arguments_json, "cols").value_or(1.0)));
    args.add_i4(static_cast<long>(number_arg(arguments_json, "rows").value_or(1.0)));
    VariantHolder result;
    if (!method_call(entity.get(), L"ArrayRectangular", args, result, error)) {
      return com_failure("Entity.ArrayRectangular", error);
    }
    auto handles = handles_from_dispatch_array(result.value);
    return core::CommandResult::success(
      "{\"copies\":" + std::to_string(handles.size()) +
      ",\"handles\":" + string_list_json(handles) +
      ",\"backend\":\"com\"}");
  }

  if (operation == "region") {
    std::vector<std::string> ids = string_array_arg(arguments_json, "entity_ids");
    if (ids.empty()) {
      if (auto single = string_arg(arguments_json, "entity_id")) {
        ids.push_back(*single);
      }
    }
    if (ids.empty()) {
      return missing_arg("entity_ids");
    }
    std::vector<ComPtr<IDispatch>> entities;
    std::vector<IDispatch*> raw_entities;
    entities.reserve(ids.size());
    raw_entities.reserve(ids.size());
    for (const auto& id : ids) {
      auto entity = object_by_handle_value(session, id, error);
      if (!entity) {
        return core::CommandResult::failure(core::ErrorCategory::not_found, error);
      }
      raw_entities.push_back(entity.get());
      entities.push_back(std::move(entity));
    }
    VariantArgs args;
    args.add_dispatch_array(raw_entities);
    VariantHolder result;
    if (!method_call(model.get(), L"AddRegion", args, result, error)) {
      return com_failure("ModelSpace.AddRegion", error);
    }
    auto handles = handles_from_dispatch_array(result.value);
    return core::CommandResult::success(
      "{\"entity_type\":\"REGION\",\"handles\":" + string_list_json(handles) +
      ",\"count\":" + std::to_string(handles.size()) +
      ",\"backend\":\"com\"}");
  }

  if (operation == "extrude" || operation == "revolve") {
    auto region = object_by_handle(session, arguments_json, error);
    if (!region) {
      return core::CommandResult::failure(
        error == "entity_id is required" ? core::ErrorCategory::invalid_arg : core::ErrorCategory::not_found,
        error);
    }
    VariantArgs args;
    const wchar_t* method = nullptr;
    if (operation == "extrude") {
      method = L"AddExtrudedSolid";
      args.add_double(number_arg(arguments_json, "taper_angle").value_or(0.0) * kPi / 180.0);
      args.add_double(number_arg(arguments_json, "height").value_or(1.0));
      args.add_dispatch(region.get());
    } else {
      method = L"AddRevolvedSolid";
      args.add_double(number_arg(arguments_json, "angle").value_or(360.0) * kPi / 180.0);
      args.add_point(
        number_arg(arguments_json, "axis_dx").value_or(0.0),
        number_arg(arguments_json, "axis_dy").value_or(1.0),
        number_arg(arguments_json, "axis_dz").value_or(0.0));
      args.add_point(
        number_arg(arguments_json, "axis_x").value_or(0.0),
        number_arg(arguments_json, "axis_y").value_or(0.0),
        0.0);
      args.add_dispatch(region.get());
    }
    VariantHolder result;
    if (!method_call(model.get(), method, args, result, error)) {
      return com_failure(operation == "extrude" ? "ModelSpace.AddExtrudedSolid" : "ModelSpace.AddRevolvedSolid", error);
    }
    auto solid = dispatch_from_variant(result.value);
    if (!solid) {
      return core::CommandResult::success("{\"entity_type\":\"3DSOLID\",\"created\":true,\"backend\":\"com\"}");
    }
    return entity_payload(solid.get(), "3DSOLID");
  }

  if (operation == "boolean") {
    auto target_id = string_arg(arguments_json, "target_id");
    if (!target_id || target_id->empty()) {
      target_id = string_arg(arguments_json, "entity_id");
    }
    if (!target_id || target_id->empty()) {
      return missing_arg("target_id");
    }
    std::string boolean_operation = "union";
    if (auto data = data_object(arguments_json)) {
      boolean_operation = protocol::extract_string_property(*data, "operation").value_or(boolean_operation);
    }
    const std::string normalized = upper_copy(boolean_operation);
    long method = 0;
    if (normalized == "UNION") {
      method = 0;
    } else if (normalized == "INTERSECT") {
      method = 1;
    } else if (normalized == "SUBTRACT") {
      method = 2;
    } else {
      return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "boolean operation must be union, intersect, or subtract");
    }
    auto target = object_by_handle_value(session, *target_id, error);
    if (!target) {
      return core::CommandResult::failure(core::ErrorCategory::not_found, error);
    }
    auto tool_ids = string_array_arg(arguments_json, "tool_ids");
    if (tool_ids.empty()) {
      if (auto tool = string_arg(arguments_json, "tool_id")) {
        tool_ids.push_back(*tool);
      }
    }
    std::vector<std::string> consumed;
    for (const auto& tool_id : tool_ids) {
      auto tool = object_by_handle_value(session, tool_id, error);
      if (!tool) {
        continue;
      }
      VariantArgs args;
      args.add_dispatch(tool.get());
      args.add_i4(method);
      VariantHolder ignored;
      if (!method_call(target.get(), L"Boolean", args, ignored, error)) {
        return com_failure("3DSolid.Boolean", error);
      }
      consumed.push_back(tool_id);
    }
    return core::CommandResult::success(
      "{\"entity_type\":\"3DSOLID\",\"handle\":" + json_string(get_string_prop(target.get(), L"Handle")) +
      ",\"operation\":" + json_string(boolean_operation) +
      ",\"tools_consumed\":" + string_list_json(consumed) +
      ",\"backend\":\"com\"}");
  }

  return core::not_supported("COM advanced entity operation is not implemented");
}

core::CommandResult entity_query(AutoCadComSession& session, std::string_view operation, std::string_view arguments_json) {
  std::string error;
  auto model = session.model_space(error);
  if (!model) {
    return com_failure("Document.ModelSpace", error);
  }
  if (operation == "count") {
    return core::CommandResult::success("{\"count\":" + std::to_string(get_long_prop(model.get(), L"Count", 0)) + "}");
  }
  if (operation == "select") {
    const long count = get_long_prop(model.get(), L"Count", 0);
    const long limit = static_cast<long>(number_arg(arguments_json, "limit").value_or(5000.0));
    const std::string layer_filter = string_arg(arguments_json, "layer").value_or("");
    std::string type_filter = string_arg(arguments_json, "type").value_or("");
    if (!type_filter.empty()) {
      type_filter = upper_copy(std::move(type_filter));
    }
    std::vector<std::string> handles;
    std::string by_layer = "{";
    std::string by_type = "{";
    for (long i = 0; i < count && static_cast<long>(handles.size()) < limit; ++i) {
      std::string item_error;
      auto entity = collection_item(model.get(), i, item_error);
      if (!entity) {
        item_error.clear();
        entity = collection_item(model.get(), i + 1, item_error);
      }
      if (!entity) {
        continue;
      }
      const std::string layer = get_string_prop(entity.get(), L"Layer");
      const std::string type = dxf_type_from_object_name(get_string_prop(entity.get(), L"ObjectName"));
      if (!layer_filter.empty() && layer != layer_filter) {
        continue;
      }
      if (!type_filter.empty() && type != type_filter) {
        continue;
      }
      handles.push_back(get_string_prop(entity.get(), L"Handle"));
    }
    by_layer += "}";
    by_type += "}";
    return core::CommandResult::success(
      "{\"count\":" + std::to_string(handles.size()) +
      ",\"handles\":" + string_list_json(handles) +
      ",\"by_layer\":" + by_layer +
      ",\"by_type\":" + by_type +
      ",\"truncated\":" + (static_cast<long>(handles.size()) >= limit ? "true" : "false") +
      ",\"backend\":\"com\"}");
  }
  if (operation == "osnap") {
    const double x = number_arg(arguments_json, "x").value_or(0.0);
    const double y = number_arg(arguments_json, "y").value_or(0.0);
    const double tolerance = number_arg(arguments_json, "tolerance").value_or(5.0);
    const auto modes = string_array_arg(arguments_json, "modes");
    auto mode_active = [&](std::string_view mode) {
      if (modes.empty()) {
        return mode == "endpoint" || mode == "midpoint" || mode == "center";
      }
      return std::find(modes.begin(), modes.end(), std::string(mode)) != modes.end();
    };
    bool found = false;
    double best_distance = 0.0;
    double best_x = 0.0;
    double best_y = 0.0;
    std::string best_mode;
    std::string best_handle;
    auto consider = [&](double cx, double cy, std::string_view mode, std::string_view handle) {
      const double distance = std::hypot(cx - x, cy - y);
      if (distance > tolerance) {
        return;
      }
      if (!found || distance < best_distance) {
        found = true;
        best_distance = distance;
        best_x = cx;
        best_y = cy;
        best_mode = std::string(mode);
        best_handle = std::string(handle);
      }
    };

    const long count = get_long_prop(model.get(), L"Count", 0);
    for (long i = 0; i < count; ++i) {
      std::string item_error;
      auto entity = collection_item(model.get(), i, item_error);
      if (!entity) {
        item_error.clear();
        entity = collection_item(model.get(), i + 1, item_error);
      }
      if (!entity) {
        continue;
      }
      const std::string type = dxf_type_from_object_name(get_string_prop(entity.get(), L"ObjectName"));
      const std::string handle = get_string_prop(entity.get(), L"Handle");
      if (type == "LINE") {
        const auto start = get_double_array_prop(entity.get(), L"StartPoint");
        const auto end = get_double_array_prop(entity.get(), L"EndPoint");
        if (start.size() >= 2 && end.size() >= 2) {
          if (mode_active("endpoint")) {
            consider(start[0], start[1], "endpoint", handle);
            consider(end[0], end[1], "endpoint", handle);
          }
          if (mode_active("midpoint")) {
            consider((start[0] + end[0]) / 2.0, (start[1] + end[1]) / 2.0, "midpoint", handle);
          }
        }
      } else if (type == "CIRCLE") {
        const auto center = get_double_array_prop(entity.get(), L"Center");
        const double radius = get_double_prop(entity.get(), L"Radius", 0.0);
        if (center.size() >= 2) {
          if (mode_active("center")) {
            consider(center[0], center[1], "center", handle);
          }
          if (mode_active("endpoint")) {
            consider(center[0] + radius, center[1], "endpoint", handle);
            consider(center[0] - radius, center[1], "endpoint", handle);
            consider(center[0], center[1] + radius, "endpoint", handle);
            consider(center[0], center[1] - radius, "endpoint", handle);
          }
        }
      } else if (type == "ARC") {
        const auto center = get_double_array_prop(entity.get(), L"Center");
        const double radius = get_double_prop(entity.get(), L"Radius", 0.0);
        const double start_angle = get_double_prop(entity.get(), L"StartAngle", 0.0);
        const double end_angle = get_double_prop(entity.get(), L"EndAngle", 0.0);
        if (center.size() >= 2) {
          if (mode_active("center")) {
            consider(center[0], center[1], "center", handle);
          }
          if (mode_active("endpoint")) {
            consider(center[0] + radius * std::cos(start_angle), center[1] + radius * std::sin(start_angle), "endpoint", handle);
            consider(center[0] + radius * std::cos(end_angle), center[1] + radius * std::sin(end_angle), "endpoint", handle);
          }
          if (mode_active("midpoint")) {
            const double mid = (start_angle + end_angle) / 2.0;
            consider(center[0] + radius * std::cos(mid), center[1] + radius * std::sin(mid), "midpoint", handle);
          }
        }
      } else if (type == "LWPOLYLINE" || type == "POLYLINE") {
        const auto coords = get_double_array_prop(entity.get(), L"Coordinates");
        for (std::size_t j = 0; j + 1 < coords.size(); j += 2) {
          if (mode_active("endpoint")) {
            consider(coords[j], coords[j + 1], "endpoint", handle);
          }
          if (mode_active("midpoint") && j + 3 < coords.size()) {
            consider((coords[j] + coords[j + 2]) / 2.0, (coords[j + 1] + coords[j + 3]) / 2.0, "midpoint", handle);
          }
        }
      }
    }
    if (!found) {
      return core::CommandResult::failure(core::ErrorCategory::not_found, "No osnap point found within tolerance");
    }
    return core::CommandResult::success(
      "{\"x\":" + std::to_string(best_x) +
      ",\"y\":" + std::to_string(best_y) +
      ",\"mode_used\":" + json_string(best_mode) +
      ",\"entity_handle\":" + json_string(best_handle) +
      ",\"distance\":" + std::to_string(best_distance) +
      ",\"tolerance\":" + std::to_string(tolerance) +
      ",\"backend\":\"com\"}");
  }
  if (operation == "list") {
    const long count = get_long_prop(model.get(), L"Count", 0);
    const long limit = static_cast<long>(number_arg(arguments_json, "limit").value_or(100.0));
    const std::string layer_filter = string_arg(arguments_json, "layer").value_or("");
    std::string entities = "[";
    bool first = true;
    for (long i = 0; i < count && i < limit; ++i) {
      std::string item_error;
      auto entity = collection_item(model.get(), i, item_error);
      if (!entity) {
        item_error.clear();
        entity = collection_item(model.get(), i + 1, item_error);
      }
      if (!entity) {
        continue;
      }
      const std::string layer = get_string_prop(entity.get(), L"Layer");
      if (!layer_filter.empty() && layer != layer_filter) {
        continue;
      }
      if (!first) entities += ",";
      first = false;
      entities += "{\"handle\":" + json_string(get_string_prop(entity.get(), L"Handle")) +
        ",\"entity_id\":" + json_string(get_string_prop(entity.get(), L"Handle")) +
        ",\"type\":" + json_string(get_string_prop(entity.get(), L"ObjectName")) +
        ",\"layer\":" + json_string(layer) + "}";
    }
    entities += "]";
    return core::CommandResult::success("{\"entities\":" + entities + ",\"count\":" + std::to_string(count) + "}");
  }
  if (operation == "get") {
    auto entity = object_by_handle(session, arguments_json, error);
    if (!entity) {
      return core::CommandResult::failure(core::ErrorCategory::not_found, error);
    }
    return entity_payload(entity.get());
  }
  return core::not_supported("COM entity query operation is not implemented");
}

core::CommandResult entity_modify(AutoCadComSession& session, std::string_view operation, std::string_view arguments_json) {
  constexpr double kPi = 3.14159265358979323846;
  std::string error;
  auto entity = object_by_handle(session, arguments_json, error);
  if (!entity) {
    return core::CommandResult::failure(
      error == "entity_id is required" ? core::ErrorCategory::invalid_arg : core::ErrorCategory::not_found,
      error);
  }

  VariantArgs args;
  const wchar_t* method = nullptr;
  if (operation == "erase") {
    method = L"Delete";
  } else if (operation == "copy") {
    method = L"Copy";
  } else if (operation == "move") {
    method = L"Move";
    args.add_point(number_arg(arguments_json, "dx").value_or(0.0), number_arg(arguments_json, "dy").value_or(0.0));
    args.add_point(0.0, 0.0);
  } else if (operation == "rotate") {
    double cx{}, cy{}, angle{};
    if (auto missing = required_numeric(arguments_json, "cx", cx); !missing.ok) return missing;
    if (auto missing = required_numeric(arguments_json, "cy", cy); !missing.ok) return missing;
    if (auto missing = required_numeric(arguments_json, "angle", angle); !missing.ok) return missing;
    method = L"Rotate";
    args.add_double(angle * kPi / 180.0);
    args.add_point(cx, cy);
  } else if (operation == "scale") {
    double cx{}, cy{}, factor{};
    if (auto missing = required_numeric(arguments_json, "cx", cx); !missing.ok) return missing;
    if (auto missing = required_numeric(arguments_json, "cy", cy); !missing.ok) return missing;
    if (auto missing = required_numeric(arguments_json, "factor", factor); !missing.ok) return missing;
    method = L"ScaleEntity";
    args.add_double(factor);
    args.add_point(cx, cy);
  } else if (operation == "mirror") {
    double x1{}, y1{}, x2{}, y2{};
    if (auto missing = required_numeric(arguments_json, "x1", x1); !missing.ok) return missing;
    if (auto missing = required_numeric(arguments_json, "y1", y1); !missing.ok) return missing;
    if (auto missing = required_numeric(arguments_json, "x2", x2); !missing.ok) return missing;
    if (auto missing = required_numeric(arguments_json, "y2", y2); !missing.ok) return missing;
    method = L"Mirror";
    args.add_point(x2, y2);
    args.add_point(x1, y1);
  } else if (operation == "offset") {
    double distance{};
    if (auto missing = required_numeric(arguments_json, "distance", distance); !missing.ok) return missing;
    method = L"Offset";
    args.add_double(distance);
  } else {
    return core::not_supported("COM entity modification operation is not implemented");
  }

  VariantHolder result;
  if (!method_call(entity.get(), method, args, result, error)) {
    return com_failure("Entity modification", error);
  }
  auto result_entity = dispatch_from_variant(result.value);
  if (result_entity) {
    return entity_payload(result_entity.get());
  }
  return core::CommandResult::success(
    std::string("{\"modified\":true,\"operation\":") + json_string(operation) +
    ",\"backend\":\"com\"}");
}

std::vector<double> points_3d_arg(std::string_view arguments_json) {
  const auto raw_points = number_array_arg(arguments_json, "points");
  std::vector<double> points;
  if (!raw_points.empty()) {
    if (raw_points.size() % 3 == 0) {
      return raw_points;
    }
    if (raw_points.size() % 2 == 0) {
      points.reserve(raw_points.size() / 2 * 3);
      for (std::size_t i = 0; i < raw_points.size(); i += 2) {
        points.push_back(raw_points[i]);
        points.push_back(raw_points[i + 1]);
        points.push_back(0.0);
      }
    }
  }
  return points;
}

core::CommandResult annotation_operation(AutoCadComSession& session, std::string_view operation, std::string_view arguments_json) {
  constexpr double kPi = 3.14159265358979323846;
  if (operation == "create_text") {
    return create_entity(session, "create_text", arguments_json);
  }
  if (operation == "create_mtext") {
    return create_entity(session, "create_mtext", arguments_json);
  }

  std::string error;
  auto model = session.model_space(error);
  if (!model) {
    return com_failure("Document.ModelSpace", error);
  }

  VariantArgs args;
  const wchar_t* method = nullptr;
  const char* type_hint = "";

  if (operation == "create_dimension_aligned" || operation == "create_dimension_linear") {
    double x1{}, y1{}, x2{}, y2{};
    if (auto missing = required_numeric(arguments_json, "x1", x1); !missing.ok) return missing;
    if (auto missing = required_numeric(arguments_json, "y1", y1); !missing.ok) return missing;
    if (auto missing = required_numeric(arguments_json, "x2", x2); !missing.ok) return missing;
    if (auto missing = required_numeric(arguments_json, "y2", y2); !missing.ok) return missing;
    const double tx = number_arg(arguments_json, "tx").value_or(number_arg(arguments_json, "text_x").value_or((x1 + x2) / 2.0));
    const double ty = number_arg(arguments_json, "ty").value_or(number_arg(arguments_json, "text_y").value_or((y1 + y2) / 2.0 + 10.0));
    if (operation == "create_dimension_linear") {
      method = L"AddDimRotated";
      args.add_double(number_arg(arguments_json, "rotation").value_or(0.0) * kPi / 180.0);
      type_hint = "DIMENSION_LINEAR";
    } else {
      method = L"AddDimAligned";
      type_hint = "DIMENSION_ALIGNED";
    }
    args.add_point(tx, ty);
    args.add_point(x2, y2);
    args.add_point(x1, y1);
  } else if (operation == "create_dimension_radius") {
    double cx{}, cy{}, x{}, y{};
    if (auto missing = required_numeric(arguments_json, "cx", cx); !missing.ok) return missing;
    if (auto missing = required_numeric(arguments_json, "cy", cy); !missing.ok) return missing;
    x = number_arg(arguments_json, "x").value_or(cx + number_arg(arguments_json, "radius").value_or(10.0));
    y = number_arg(arguments_json, "y").value_or(cy);
    method = L"AddDimRadial";
    type_hint = "DIMENSION_RADIUS";
    args.add_double(number_arg(arguments_json, "leader_length").value_or(2.0));
    args.add_point(x, y);
    args.add_point(cx, cy);
  } else if (operation == "create_dimension_angular") {
    double cx{}, cy{}, x1{}, y1{}, x2{}, y2{};
    if (auto missing = required_numeric(arguments_json, "cx", cx); !missing.ok) return missing;
    if (auto missing = required_numeric(arguments_json, "cy", cy); !missing.ok) return missing;
    if (auto missing = required_numeric(arguments_json, "x1", x1); !missing.ok) return missing;
    if (auto missing = required_numeric(arguments_json, "y1", y1); !missing.ok) return missing;
    if (auto missing = required_numeric(arguments_json, "x2", x2); !missing.ok) return missing;
    if (auto missing = required_numeric(arguments_json, "y2", y2); !missing.ok) return missing;
    const double tx = number_arg(arguments_json, "tx").value_or(number_arg(arguments_json, "text_x").value_or(cx + 10.0));
    const double ty = number_arg(arguments_json, "ty").value_or(number_arg(arguments_json, "text_y").value_or(cy + 10.0));
    method = L"AddDimAngular";
    type_hint = "DIMENSION_ANGULAR";
    args.add_point(tx, ty);
    args.add_point(x2, y2);
    args.add_point(x1, y1);
    args.add_point(cx, cy);
  } else if (operation == "create_leader") {
    auto points = points_3d_arg(arguments_json);
    if (points.size() < 6 || points.size() % 3 != 0) {
      return core::CommandResult::failure(core::ErrorCategory::invalid_arg, "points must contain at least two 2D or 3D points");
    }
    method = L"AddLeader";
    type_hint = "LEADER";
    args.add_i4(static_cast<long>(number_arg(arguments_json, "leader_type").value_or(0.0)));
    args.add_empty();
    args.add_double_array(points);
  } else if (operation == "create_table") {
    double x{}, y{};
    if (auto missing = required_numeric(arguments_json, "x", x); !missing.ok) return missing;
    if (auto missing = required_numeric(arguments_json, "y", y); !missing.ok) return missing;
    method = L"AddTable";
    type_hint = "TABLE";
    args.add_double(number_arg(arguments_json, "col_width").value_or(2.0));
    args.add_double(number_arg(arguments_json, "row_height").value_or(0.5));
    args.add_i4(static_cast<long>(number_arg(arguments_json, "cols").value_or(1.0)));
    args.add_i4(static_cast<long>(number_arg(arguments_json, "rows").value_or(1.0)));
    args.add_point(x, y);
  } else {
    return core::not_supported("COM annotation operation is not implemented");
  }

  VariantHolder result;
  if (!method_call(model.get(), method, args, result, error)) {
    return com_failure("ModelSpace annotation creation", error);
  }
  auto entity = dispatch_from_variant(result.value);
  if (!entity) {
    return core::CommandResult::success("{\"created\":true,\"backend\":\"com\"}");
  }
  if (auto layer = string_arg(arguments_json, "layer"); layer && !layer->empty()) {
    VariantArgs put_args;
    put_args.add_bstr(*layer);
    std::string layer_error;
    (void)property_put(entity.get(), L"Layer", put_args, layer_error);
  }
  return entity_payload(entity.get(), type_hint);
}

core::CommandResult layer_operation(AutoCadComSession& session, std::string_view operation, std::string_view arguments_json) {
  std::string error;
  auto doc = session.active_document(error);
  if (!doc) {
    return com_failure("ActiveDocument", error);
  }
  auto layers = named_collection(doc.get(), L"Layers", error);
  if (!layers) {
    return com_failure("Document.Layers", error);
  }
  if (operation == "list") {
    return core::CommandResult::success("{\"layers\":" + layers_json(layers.get()) + ",\"backend\":\"com\"}");
  }

  auto name = string_arg(arguments_json, "name");
  if (!name || name->empty()) {
    return missing_arg("name");
  }

  ComPtr<IDispatch> layer;
  if (operation == "create") {
    VariantArgs args;
    args.add_bstr(*name);
    VariantHolder result;
    if (!method_call(layers.get(), L"Add", args, result, error)) {
      return com_failure("Layers.Add", error);
    }
    layer = dispatch_from_variant(result.value);
  } else {
    layer = collection_item(layers.get(), *name, error);
    if (!layer) {
      return core::CommandResult::failure(core::ErrorCategory::not_found, error);
    }
  }

  if (operation == "set_current") {
    VariantArgs args;
    args.add_dispatch(layer.get());
    if (!property_put(doc.get(), L"ActiveLayer", args, error)) {
      return com_failure("Document.ActiveLayer", error);
    }
    return layer_payload(layer.get(), "set_current");
  }

  if (operation == "create" || operation == "set_properties") {
    if (auto color = number_arg(arguments_json, "color")) {
      VariantArgs put_args;
      put_args.add_i4(static_cast<long>(*color));
      std::string put_error;
      if (!property_put(layer.get(), L"Color", put_args, put_error)) {
        return com_failure("Layer.Color", put_error);
      }
    }
    if (auto linetype = string_arg(arguments_json, "linetype"); linetype && !linetype->empty()) {
      VariantArgs put_args;
      put_args.add_bstr(*linetype);
      std::string put_error;
      (void)property_put(layer.get(), L"Linetype", put_args, put_error);
    }
    return layer_payload(layer.get(), operation == "create" ? "create" : "set_properties");
  }

  if (operation == "freeze" || operation == "thaw" || operation == "lock" || operation == "unlock") {
    VariantArgs put_args;
    put_args.add_bool(operation == "freeze" || operation == "lock");
    if (!property_put(layer.get(), operation == "freeze" || operation == "thaw" ? L"Freeze" : L"Lock", put_args, error)) {
      return com_failure("Layer state update", error);
    }
    return layer_payload(layer.get(), operation);
  }

  return core::not_supported("COM layer operation is not implemented");
}

core::CommandResult block_operation(AutoCadComSession& session, std::string_view operation, std::string_view arguments_json) {
  std::string error;
  auto doc = session.active_document(error);
  if (!doc) {
    return com_failure("ActiveDocument", error);
  }
  if (operation == "list") {
    auto blocks = named_collection(doc.get(), L"Blocks", error);
    if (!blocks) {
      return com_failure("Document.Blocks", error);
    }
    return core::CommandResult::success(collection_names_json(blocks.get(), "blocks"));
  }
  if (operation == "insert" || operation == "insert_with_attributes") {
    auto name = string_arg(arguments_json, "name");
    if (!name) {
      name = string_arg(arguments_json, "block_id");
    }
    double x{}, y{};
    if (!name || name->empty()) return missing_arg("name");
    if (auto missing = required_numeric(arguments_json, "x", x); !missing.ok) return missing;
    if (auto missing = required_numeric(arguments_json, "y", y); !missing.ok) return missing;
    auto model = session.model_space(error);
    if (!model) {
      return com_failure("Document.ModelSpace", error);
    }
    const double scale = number_arg(arguments_json, "scale").value_or(1.0);
    const double rotation = number_arg(arguments_json, "rotation").value_or(0.0) * 3.14159265358979323846 / 180.0;
    VariantArgs args;
    args.add_double(rotation);
    args.add_double(scale);
    args.add_double(scale);
    args.add_double(scale);
    args.add_bstr(*name);
    args.add_point(x, y);
    VariantHolder result;
    if (!method_call(model.get(), L"InsertBlock", args, result, error)) {
      return com_failure("ModelSpace.InsertBlock", error);
    }
    auto entity = dispatch_from_variant(result.value);
    if (!entity) {
      return core::CommandResult::success("{\"inserted\":true,\"backend\":\"com\"}");
    }
    return entity_payload(entity.get(), "INSERT");
  }
  return core::not_supported("COM block operation is not implemented");
}

bool block_is_xref(IDispatch* block) {
  return get_bool_prop(block, L"IsXRef", false);
}

std::string xrefs_json(IDispatch* blocks, bool only_broken) {
  const long count = get_long_prop(blocks, L"Count", 0);
  std::string items = "[";
  bool first = true;
  std::string ignored;
  for (long i = 0; i < count; ++i) {
    auto block = collection_item(blocks, i, ignored);
    if (!block) {
      ignored.clear();
      block = collection_item(blocks, i + 1, ignored);
    }
    if (!block || !block_is_xref(block.get())) {
      continue;
    }
    const std::string path = get_string_prop(block.get(), L"Path");
    const bool broken = path.empty() || !std::filesystem::exists(std::filesystem::path(path));
    if (only_broken && !broken) {
      continue;
    }
    if (!first) {
      items += ",";
    }
    first = false;
    items += "{\"name\":" + json_string(get_string_prop(block.get(), L"Name")) +
      ",\"path\":" + json_string(path) +
      ",\"broken\":" + (broken ? "true" : "false") +
      "}";
  }
  items += "]";
  return items;
}

core::CommandResult project_operation(AutoCadComSession& session, std::string_view operation, std::string_view arguments_json) {
  std::string error;
  auto doc = session.active_document(error);
  if (!doc) {
    return com_failure("ActiveDocument", error);
  }
  if (operation == "list_layers") {
    auto layers = named_collection(doc.get(), L"Layers", error);
    if (!layers) {
      return com_failure("Document.Layers", error);
    }
    return core::CommandResult::success("{\"layers\":" + layers_json(layers.get()) + ",\"backend\":\"com\"}");
  }
  if (operation == "list_blocks") {
    auto blocks = named_collection(doc.get(), L"Blocks", error);
    if (!blocks) {
      return com_failure("Document.Blocks", error);
    }
    return core::CommandResult::success(collection_names_json(blocks.get(), "blocks"));
  }
  if (operation == "list_xrefs" || operation == "find_broken_xrefs") {
    auto blocks = named_collection(doc.get(), L"Blocks", error);
    if (!blocks) {
      return com_failure("Document.Blocks", error);
    }
    return core::CommandResult::success(
      std::string("{\"xrefs\":") + xrefs_json(blocks.get(), operation == "find_broken_xrefs") +
      ",\"backend\":\"com\"}");
  }

  if (operation == "xref_attach") {
    auto path = string_arg(arguments_json, "path");
    auto name = string_arg(arguments_json, "name").value_or("");
    if (!path || path->empty()) {
      return missing_arg("path");
    }
    if (name.empty()) {
      name = std::filesystem::path(*path).stem().string();
    }
    auto model = session.model_space(error);
    if (!model) {
      return com_failure("Document.ModelSpace", error);
    }
    const double x = number_arg(arguments_json, "x").value_or(0.0);
    const double y = number_arg(arguments_json, "y").value_or(0.0);
    const double scale = number_arg(arguments_json, "scale").value_or(1.0);
    const double rotation = number_arg(arguments_json, "rotation").value_or(0.0) * 3.14159265358979323846 / 180.0;
    VariantArgs args;
    args.add_bool(bool_arg(arguments_json, "overlay", false));
    args.add_double(rotation);
    args.add_double(scale);
    args.add_double(scale);
    args.add_double(scale);
    args.add_point(x, y);
    args.add_bstr(name);
    args.add_bstr(*path);
    VariantHolder result;
    if (!method_call(model.get(), L"AttachExternalReference", args, result, error)) {
      return com_failure("ModelSpace.AttachExternalReference", error);
    }
    auto entity = dispatch_from_variant(result.value);
    if (entity) {
      return entity_payload(entity.get(), "XREF");
    }
    return core::CommandResult::success(
      "{\"attached\":true,\"name\":" + json_string(name) +
      ",\"path\":" + json_string(*path) +
      ",\"backend\":\"com\"}");
  }

  if (operation == "xref_detach" || operation == "xref_reload" || operation == "xref_bind") {
    auto name = string_arg(arguments_json, "name");
    if (!name || name->empty()) {
      return missing_arg("name");
    }
    auto blocks = named_collection(doc.get(), L"Blocks", error);
    if (!blocks) {
      return com_failure("Document.Blocks", error);
    }
    auto block = collection_item(blocks.get(), *name, error);
    if (!block) {
      return core::CommandResult::failure(core::ErrorCategory::not_found, error);
    }
    VariantArgs args;
    const wchar_t* method = nullptr;
    if (operation == "xref_detach") {
      method = L"Detach";
    } else if (operation == "xref_reload") {
      method = L"Reload";
    } else {
      method = L"Bind";
      args.add_bool(bool_arg(arguments_json, "insert_bind", false));
    }
    VariantHolder ignored;
    if (!method_call(block.get(), method, args, ignored, error)) {
      return com_failure("XREF operation", error);
    }
    return core::CommandResult::success(
      std::string("{\"xref\":") + json_string(*name) +
      ",\"operation\":" + json_string(operation) +
      ",\"backend\":\"com\"}");
  }

  return core::not_supported("COM project operation is not implemented");
}

std::string base64_encode(const std::vector<BYTE>& bytes) {
  static constexpr char kAlphabet[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  std::string out;
  out.reserve(((bytes.size() + 2) / 3) * 4);
  for (std::size_t i = 0; i < bytes.size(); i += 3) {
    const unsigned int b0 = bytes[i];
    const unsigned int b1 = (i + 1 < bytes.size()) ? bytes[i + 1] : 0;
    const unsigned int b2 = (i + 2 < bytes.size()) ? bytes[i + 2] : 0;
    out += kAlphabet[(b0 >> 2) & 0x3f];
    out += kAlphabet[((b0 & 0x03) << 4) | ((b1 >> 4) & 0x0f)];
    out += (i + 1 < bytes.size()) ? kAlphabet[((b1 & 0x0f) << 2) | ((b2 >> 6) & 0x03)] : '=';
    out += (i + 2 < bytes.size()) ? kAlphabet[b2 & 0x3f] : '=';
  }
  return out;
}

struct WindowDc {
  HWND hwnd{nullptr};
  HDC hdc{nullptr};
  ~WindowDc() {
    if (hdc != nullptr) {
      ReleaseDC(hwnd, hdc);
    }
  }
};

struct MemoryDc {
  HDC hdc{nullptr};
  ~MemoryDc() {
    if (hdc != nullptr) {
      DeleteDC(hdc);
    }
  }
};

struct BitmapHandle {
  HBITMAP bitmap{nullptr};
  ~BitmapHandle() {
    if (bitmap != nullptr) {
      DeleteObject(bitmap);
    }
  }
};

std::optional<std::vector<BYTE>> encode_bitmap_as_png(HBITMAP bitmap, int width, int height, std::string& error) {
  ComPtr<IWICImagingFactory> factory;
  HRESULT hr = CoCreateInstance(
    CLSID_WICImagingFactory,
    nullptr,
    CLSCTX_INPROC_SERVER,
    IID_IWICImagingFactory,
    reinterpret_cast<void**>(factory.put()));
  if (FAILED(hr) || !factory) {
    error = "CoCreateInstance(IWICImagingFactory) failed: " + hresult_hex(hr);
    return std::nullopt;
  }

  ComPtr<IWICBitmap> wic_bitmap;
  hr = factory.get()->CreateBitmapFromHBITMAP(bitmap, nullptr, WICBitmapIgnoreAlpha, wic_bitmap.put());
  if (FAILED(hr) || !wic_bitmap) {
    error = "CreateBitmapFromHBITMAP failed: " + hresult_hex(hr);
    return std::nullopt;
  }

  ComPtr<IStream> stream;
  hr = CreateStreamOnHGlobal(nullptr, TRUE, stream.put());
  if (FAILED(hr) || !stream) {
    error = "CreateStreamOnHGlobal failed: " + hresult_hex(hr);
    return std::nullopt;
  }

  ComPtr<IWICBitmapEncoder> encoder;
  hr = factory.get()->CreateEncoder(GUID_ContainerFormatPng, nullptr, encoder.put());
  if (FAILED(hr) || !encoder) {
    error = "CreateEncoder(PNG) failed: " + hresult_hex(hr);
    return std::nullopt;
  }
  hr = encoder.get()->Initialize(stream.get(), WICBitmapEncoderNoCache);
  if (FAILED(hr)) {
    error = "PNG encoder Initialize failed: " + hresult_hex(hr);
    return std::nullopt;
  }

  ComPtr<IWICBitmapFrameEncode> frame;
  hr = encoder.get()->CreateNewFrame(frame.put(), nullptr);
  if (FAILED(hr) || !frame) {
    error = "CreateNewFrame failed: " + hresult_hex(hr);
    return std::nullopt;
  }
  hr = frame.get()->Initialize(nullptr);
  if (FAILED(hr)) {
    error = "Frame Initialize failed: " + hresult_hex(hr);
    return std::nullopt;
  }
  hr = frame.get()->SetSize(static_cast<UINT>(width), static_cast<UINT>(height));
  if (FAILED(hr)) {
    error = "Frame SetSize failed: " + hresult_hex(hr);
    return std::nullopt;
  }
  WICPixelFormatGUID pixel_format = GUID_WICPixelFormat32bppBGRA;
  hr = frame.get()->SetPixelFormat(&pixel_format);
  if (FAILED(hr)) {
    error = "Frame SetPixelFormat failed: " + hresult_hex(hr);
    return std::nullopt;
  }
  hr = frame.get()->WriteSource(wic_bitmap.get(), nullptr);
  if (FAILED(hr)) {
    error = "Frame WriteSource failed: " + hresult_hex(hr);
    return std::nullopt;
  }
  hr = frame.get()->Commit();
  if (FAILED(hr)) {
    error = "Frame Commit failed: " + hresult_hex(hr);
    return std::nullopt;
  }
  hr = encoder.get()->Commit();
  if (FAILED(hr)) {
    error = "Encoder Commit failed: " + hresult_hex(hr);
    return std::nullopt;
  }

  STATSTG stat{};
  hr = stream.get()->Stat(&stat, STATFLAG_NONAME);
  if (FAILED(hr)) {
    error = "Stream Stat failed: " + hresult_hex(hr);
    return std::nullopt;
  }
  if (stat.cbSize.QuadPart <= 0 || stat.cbSize.QuadPart > 100 * 1024 * 1024) {
    error = "Unexpected PNG stream size";
    return std::nullopt;
  }
  LARGE_INTEGER zero{};
  hr = stream.get()->Seek(zero, STREAM_SEEK_SET, nullptr);
  if (FAILED(hr)) {
    error = "Stream Seek failed: " + hresult_hex(hr);
    return std::nullopt;
  }
  std::vector<BYTE> bytes(static_cast<std::size_t>(stat.cbSize.QuadPart));
  ULONG read = 0;
  hr = stream.get()->Read(bytes.data(), static_cast<ULONG>(bytes.size()), &read);
  if (FAILED(hr) || read != bytes.size()) {
    error = "Stream Read failed: " + hresult_hex(hr);
    return std::nullopt;
  }
  return bytes;
}

core::CommandResult capture_window_png_base64(HWND hwnd) {
  if (hwnd == nullptr || !IsWindow(hwnd)) {
    return core::CommandResult::failure(core::ErrorCategory::not_found, "AutoCAD window handle is not valid");
  }

  RECT rect{};
  WINDOWPLACEMENT placement{};
  placement.length = sizeof(WINDOWPLACEMENT);
  if (GetWindowPlacement(hwnd, &placement) && IsIconic(hwnd)) {
    rect = placement.rcNormalPosition;
  } else if (!GetWindowRect(hwnd, &rect)) {
    return core::CommandResult::failure(core::ErrorCategory::internal_error, "GetWindowRect failed");
  }
  const int width = rect.right - rect.left;
  const int height = rect.bottom - rect.top;
  if (width <= 0 || height <= 0) {
    return core::CommandResult::failure(core::ErrorCategory::internal_error, "AutoCAD window has invalid capture dimensions");
  }

  WindowDc window_dc{hwnd, GetWindowDC(hwnd)};
  if (window_dc.hdc == nullptr) {
    return core::CommandResult::failure(core::ErrorCategory::internal_error, "GetWindowDC failed");
  }
  MemoryDc memory_dc{CreateCompatibleDC(window_dc.hdc)};
  if (memory_dc.hdc == nullptr) {
    return core::CommandResult::failure(core::ErrorCategory::internal_error, "CreateCompatibleDC failed");
  }
  BitmapHandle bitmap{CreateCompatibleBitmap(window_dc.hdc, width, height)};
  if (bitmap.bitmap == nullptr) {
    return core::CommandResult::failure(core::ErrorCategory::internal_error, "CreateCompatibleBitmap failed");
  }
  HGDIOBJ previous = SelectObject(memory_dc.hdc, bitmap.bitmap);
  const BOOL printed = PrintWindow(hwnd, memory_dc.hdc, 0x00000002);
  if (printed != TRUE) {
    (void)BitBlt(memory_dc.hdc, 0, 0, width, height, window_dc.hdc, 0, 0, SRCCOPY);
  }
  if (previous != nullptr) {
    SelectObject(memory_dc.hdc, previous);
  }

  std::string error;
  auto png = encode_bitmap_as_png(bitmap.bitmap, width, height, error);
  if (!png) {
    return core::CommandResult::failure(core::ErrorCategory::internal_error, error);
  }
  return core::CommandResult::success(json_string(base64_encode(*png)));
}

core::CommandResult screenshot_operation(AutoCadComSession& session) {
  HWND hwnd = reinterpret_cast<HWND>(static_cast<std::uintptr_t>(get_long_prop(session.app(), L"HWND", 0)));
  if (hwnd == nullptr || !IsWindow(hwnd)) {
    auto window = find_autocad_window("");
    if (window) {
      hwnd = reinterpret_cast<HWND>(window->hwnd);
    }
  }
  return capture_window_png_base64(hwnd);
}

core::CommandResult view_operation(AutoCadComSession& session, std::string_view operation, std::string_view arguments_json) {
  std::string error;
  VariantArgs args;
  const wchar_t* method = nullptr;
  if (operation == "get_screenshot") {
    (void)arguments_json;
    return screenshot_operation(session);
  }
  if (operation == "zoom_extents") {
    method = L"ZoomExtents";
  } else if (operation == "zoom_window") {
    double x1{}, y1{}, x2{}, y2{};
    if (auto missing = required_numeric(arguments_json, "x1", x1); !missing.ok) return missing;
    if (auto missing = required_numeric(arguments_json, "y1", y1); !missing.ok) return missing;
    if (auto missing = required_numeric(arguments_json, "x2", x2); !missing.ok) return missing;
    if (auto missing = required_numeric(arguments_json, "y2", y2); !missing.ok) return missing;
    method = L"ZoomWindow";
    args.add_point(x2, y2);
    args.add_point(x1, y1);
  } else {
    return core::not_supported("COM view operation is not implemented");
  }
  VariantHolder ignored;
  if (!method_call(session.app(), method, args, ignored, error)) {
    return com_failure("Application zoom", error);
  }
  return core::CommandResult::success(
    std::string("{\"zoomed\":true,\"operation\":") + json_string(operation) + ",\"backend\":\"com\"}");
}

core::CommandResult style_payload(IDispatch* style, std::string_view kind, std::string_view action) {
  return core::CommandResult::success(
    std::string("{\"action\":") + json_string(action) +
    ",\"kind\":" + json_string(kind) +
    ",\"name\":" + json_string(get_string_prop(style, L"Name")) +
    ",\"font_file\":" + json_string(get_string_prop(style, L"FontFile")) +
    ",\"height\":" + std::to_string(get_long_prop(style, L"Height", 0)) +
    ",\"backend\":\"com\"}");
}

core::CommandResult style_operation(AutoCadComSession& session, std::string_view operation, std::string_view arguments_json) {
  std::string error;
  auto doc = session.active_document(error);
  if (!doc) {
    return com_failure("ActiveDocument", error);
  }

  const wchar_t* collection_name = nullptr;
  const wchar_t* active_property = nullptr;
  std::string kind;
  if (operation.find("dimstyle") != std::string_view::npos) {
    collection_name = L"DimStyles";
    active_property = L"ActiveDimStyle";
    kind = "dimstyle";
  } else if (operation.find("textstyle") != std::string_view::npos) {
    collection_name = L"TextStyles";
    active_property = L"ActiveTextStyle";
    kind = "textstyle";
  } else if (operation.find("mleaderstyle") != std::string_view::npos) {
    collection_name = L"MLeaderStyles";
    active_property = L"ActiveMLeaderStyle";
    kind = "mleaderstyle";
  } else {
    return core::not_supported("COM style operation is not implemented");
  }

  auto collection = named_collection(doc.get(), collection_name, error);
  if (!collection) {
    return com_failure("Document style collection", error);
  }

  if (operation.rfind("list_", 0) == 0) {
    return core::CommandResult::success(collection_names_json(collection.get(), kind + "s"));
  }

  auto name = string_arg(arguments_json, "name").value_or("");
  if (name.empty() && operation.rfind("set_current_", 0) == 0) {
    name = string_arg(arguments_json, kind).value_or("");
  }
  if (name.empty()) {
    return missing_arg("name");
  }

  ComPtr<IDispatch> style;
  if (operation.rfind("create_", 0) == 0) {
    VariantArgs args;
    args.add_bstr(name);
    VariantHolder result;
    if (!method_call(collection.get(), L"Add", args, result, error)) {
      return com_failure("StyleCollection.Add", error);
    }
    style = dispatch_from_variant(result.value);
  } else {
    style = collection_item(collection.get(), name, error);
    if (!style) {
      return core::CommandResult::failure(core::ErrorCategory::not_found, error);
    }
  }

  if (kind == "textstyle" && operation.rfind("create_", 0) == 0) {
    if (auto font = string_arg(arguments_json, "font"); font && !font->empty()) {
      VariantArgs put_args;
      put_args.add_bstr(*font);
      std::string put_error;
      (void)property_put(style.get(), L"FontFile", put_args, put_error);
    }
    if (auto height = number_arg(arguments_json, "height")) {
      VariantArgs put_args;
      put_args.add_double(*height);
      std::string put_error;
      (void)property_put(style.get(), L"Height", put_args, put_error);
    }
  }

  if (operation.rfind("set_current_", 0) == 0) {
    VariantArgs args;
    args.add_dispatch(style.get());
    if (!property_put(doc.get(), active_property, args, error)) {
      return com_failure("Document active style update", error);
    }
    return style_payload(style.get(), kind, "set_current");
  }

  return style_payload(style.get(), kind, operation.rfind("create_", 0) == 0 ? "create" : "get");
}

#endif

}  // namespace

ComProbeResult probe_autocad_application() {
  ComProbeResult result;
  result.prog_id = "AutoCAD.Application";
#if defined(_WIN32)
  ComApartment apartment;
  if (FAILED(apartment.result())) {
    result.error = "CoInitializeEx failed: " + hresult_hex(apartment.result());
    return result;
  }

  CLSID clsid{};
  HRESULT hr = CLSIDFromProgID(L"AutoCAD.Application", &clsid);
  if (FAILED(hr)) {
    result.error = "AutoCAD.Application ProgID is not registered: " + hresult_hex(hr);
    return result;
  }

  ComPtr<IUnknown> unknown;
  hr = GetActiveObject(clsid, nullptr, unknown.put());
  if (FAILED(hr)) {
    result.error = "No running AutoCAD COM application found: " + hresult_hex(hr);
    return result;
  }

  ComPtr<IDispatch> dispatch;
  hr = unknown.get()->QueryInterface(IID_IDispatch, reinterpret_cast<void**>(dispatch.put()));
  if (FAILED(hr) || !dispatch) {
    result.error = "Running AutoCAD object does not expose IDispatch: " + hresult_hex(hr);
    return result;
  }

  LPOLESTR name_prop = const_cast<LPOLESTR>(L"Name");
  LPOLESTR version_prop = const_cast<LPOLESTR>(L"Version");
  result.name = get_string_property(dispatch.get(), name_prop);
  result.version = get_string_property(dispatch.get(), version_prop);
  result.available = true;
  return result;
#else
  result.error = "COM automation is only available on Windows";
  return result;
#endif
}

core::CommandResult execute_autocad_com_operation(
    std::string_view tool_name,
    std::string_view operation,
    std::string_view arguments_json) {
#if defined(_WIN32)
  AutoCadComSession session;
  if (!session.attach()) {
    return attach_failure(session);
  }

  if (tool_name == "drawing") {
    if (operation == "info") return drawing_info(session);
    if (operation == "create" || operation == "open") return drawing_create_or_open(session, operation, arguments_json);
    if (operation == "save" || operation == "save_as_dxf" || operation == "save_as_dwg") {
      return drawing_save(session, operation, arguments_json);
    }
    if (operation == "get_variables") return drawing_variables(session);
    if (operation == "plot_pdf") return drawing_plot_pdf(session, arguments_json);
    if (operation == "list_layouts" || operation == "create_layout" ||
        operation == "set_active_layout" || operation == "create_viewport") {
      return drawing_layout_operation(session, operation, arguments_json);
    }
    if (operation == "purge") return send_document_command(session, "-PURGE\nALL\n*\nN\n");
    if (operation == "audit") return send_document_command(session, "AUDIT\nY\n");
    if (operation == "recover") {
      if (auto path = string_arg(arguments_json, "path"); path && !path->empty()) {
        return send_document_command(session, "RECOVER\n" + *path + "\n");
      }
      return send_document_command(session, "AUDIT\nY\n");
    }
    if (operation == "undo") return send_document_command(session, "UNDO\n1\n");
    if (operation == "redo") return send_document_command(session, "REDO\n");
  }

  if (tool_name == "entity") {
    if (operation == "create_hatch" || operation == "array" || operation == "region" ||
        operation == "extrude" || operation == "revolve" || operation == "boolean") {
      return entity_advanced(session, operation, arguments_json);
    }
    if (operation == "create_line" || operation == "create_circle" || operation == "create_text" ||
        operation == "create_mtext" || operation == "create_polyline" || operation == "create_rectangle" ||
        operation == "create_arc" || operation == "create_ellipse") {
      return create_entity(session, operation, arguments_json);
    }
    if (operation == "select" || operation == "osnap" || operation == "list" || operation == "count" || operation == "get") {
      return entity_query(session, operation, arguments_json);
    }
    if (operation == "erase" || operation == "copy" || operation == "move" || operation == "rotate" ||
        operation == "scale" || operation == "mirror" || operation == "offset") {
      return entity_modify(session, operation, arguments_json);
    }
  }

  if (tool_name == "annotation") {
    return annotation_operation(session, operation, arguments_json);
  }

  if (tool_name == "layer") {
    return layer_operation(session, operation, arguments_json);
  }

  if (tool_name == "block") {
    return block_operation(session, operation, arguments_json);
  }

  if (tool_name == "project") {
    return project_operation(session, operation, arguments_json);
  }

  if (tool_name == "view") {
    return view_operation(session, operation, arguments_json);
  }

  if (tool_name == "style") {
    return style_operation(session, operation, arguments_json);
  }

  if (tool_name == "system" && operation == "execute_lisp") {
    auto code = string_arg(arguments_json, "code").value_or("");
    if (code.empty()) {
      code = string_arg(arguments_json, "lisp").value_or("");
    }
    if (code.empty()) {
      return missing_arg("code");
    }
    return send_document_command(session, code + "\n");
  }

  return core::not_supported(
    std::string("com.") + std::string(tool_name) + "." + std::string(operation) +
    " is not implemented in the native COM operation layer yet");
#else
  (void)tool_name;
  (void)operation;
  (void)arguments_json;
  return core::CommandResult::failure(
    core::ErrorCategory::backend_unavailable,
    "COM automation is only available on Windows");
#endif
}

}  // namespace autocad_mcp::platform::win32
