from __future__ import annotations

import winreg as _winreg
from enum import Enum, Flag
from typing import Any, cast

# winreg is available only on Windows; Linux type analyzers expose an
# intentionally incomplete platform stub. Keep the runtime module dynamic.
reg = cast(Any, _winreg)


class RegistryError(Exception):
    pass


class ValueNotFound(RegistryError):
    pass


class Access(Flag):
    KEY_READ = reg.KEY_READ
    KEY_WRITE = reg.KEY_WRITE
    KEY_NOTIFY = reg.KEY_NOTIFY
    KEY_EXECUTE = reg.KEY_EXECUTE
    KEY_SET_VALUE = reg.KEY_SET_VALUE
    KEY_ALL_ACCESS = reg.KEY_ALL_ACCESS
    KEY_CREATE_LINK = reg.KEY_CREATE_LINK
    KEY_QUERY_VALUE = reg.KEY_QUERY_VALUE
    KEY_CREATE_SUB_KEY = reg.KEY_CREATE_SUB_KEY
    KEY_ENUMERATE_SUB_KEYS = reg.KEY_ENUMERATE_SUB_KEYS


class MainKey(Enum):
    HKU = reg.HKEY_USERS
    HKCR = reg.HKEY_CLASSES_ROOT
    HKCU = reg.HKEY_CURRENT_USER
    HKLM = reg.HKEY_LOCAL_MACHINE
    HKEY_USERS = reg.HKEY_USERS
    HKEY_CLASSES_ROOT = reg.HKEY_CLASSES_ROOT
    HKEY_CURRENT_USER = reg.HKEY_CURRENT_USER
    HKEY_LOCAL_MACHINE = reg.HKEY_LOCAL_MACHINE
    HKEY_CURRENT_CONFIG = reg.HKEY_CURRENT_CONFIG
    HKEY_PERFORMANCE_DATA = reg.HKEY_PERFORMANCE_DATA


class ValueType(Enum):
    REG_SZ = reg.REG_SZ
    REG_NONE = reg.REG_NONE
    REG_LINK = reg.REG_LINK
    REG_DWORD = reg.REG_DWORD
    REG_QWORD = reg.REG_QWORD
    REG_BINARY = reg.REG_BINARY
    REG_MULTI_SZ = reg.REG_MULTI_SZ
    REG_EXPAND_SZ = reg.REG_EXPAND_SZ
    REG_RESOURCE_LIST = reg.REG_RESOURCE_LIST
    REG_DWORD_BIG_ENDIAN = reg.REG_DWORD_BIG_ENDIAN
    REG_DWORD_LITTLE_ENDIAN = reg.REG_DWORD_LITTLE_ENDIAN
    REG_QWORD_LITTLE_ENDIAN = reg.REG_QWORD_LITTLE_ENDIAN
    REG_FULL_RESOURCE_DESCRIPTOR = reg.REG_FULL_RESOURCE_DESCRIPTOR
    REG_RESOURCE_REQUIREMENTS_LIST = reg.REG_RESOURCE_REQUIREMENTS_LIST


class RegistryKey:
    def __init__(self, path: str, *, read_only: bool = False) -> None:
        main_key, _, path = path.replace('/', '\\').partition('\\')
        self.main_key = MainKey[main_key]
        self.path = path
        access_flags = Access.KEY_QUERY_VALUE
        if not read_only:
            access_flags |= Access.KEY_SET_VALUE
        self._handle = reg.OpenKey(self.main_key.value, path, access=access_flags.value)

    def __enter__(self) -> RegistryKey:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self._handle.Close()

    def get(self, name: str) -> tuple[ValueType, Any]:
        try:
            value, value_type = reg.QueryValueEx(self._handle, name)
        except FileNotFoundError:
            # TODO: consider returning None for missing values
            raise ValueNotFound(name)
        return (ValueType(value_type), value)

    def set(self, name: str, value_type: ValueType, value: Any) -> None:
        reg.SetValueEx(self._handle, name, 0, value_type.value, value)

    def delete(self, name: str, *, silent: bool = False) -> bool:
        try:
            reg.DeleteValue(self._handle, name)
        except FileNotFoundError:
            if not silent:
                raise ValueNotFound(name)
            return False
        return True
