"""修改密码功能测试。

覆盖：
- ChangePasswordSchema 校验
- UserModel 密码设置 / 校验
- 密码修改逻辑（服务层）
"""
import pytest

from app.schemas.user_schema import ChangePasswordSchema
from app.models.user import UserModel


# ==================== Schema 校验测试 ====================

class TestChangePasswordSchema:
    """ChangePasswordSchema 字段校验。"""

    def test_valid_data(self):
        data = ChangePasswordSchema(
            old_password="old123456",
            new_password="new123456",
            confirm_password="new123456",
        )
        assert data.old_password == "old123456"
        assert data.new_password == "new123456"
        assert data.confirm_password == "new123456"

    def test_old_password_too_short(self):
        with pytest.raises(Exception):
            ChangePasswordSchema(
                old_password="123",
                new_password="new123456",
                confirm_password="new123456",
            )

    def test_old_password_too_long(self):
        with pytest.raises(Exception):
            ChangePasswordSchema(
                old_password="a" * 16,
                new_password="new123456",
                confirm_password="new123456",
            )

    def test_new_password_too_short(self):
        with pytest.raises(Exception):
            ChangePasswordSchema(
                old_password="old123456",
                new_password="123",
                confirm_password="123",
            )

    def test_new_password_too_long(self):
        with pytest.raises(Exception):
            ChangePasswordSchema(
                old_password="old123456",
                new_password="a" * 16,
                confirm_password="a" * 16,
            )

    def test_confirm_password_too_short(self):
        with pytest.raises(Exception):
            ChangePasswordSchema(
                old_password="old123456",
                new_password="new123456",
                confirm_password="123",
            )

    def test_extra_field_rejected(self):
        with pytest.raises(Exception):
            ChangePasswordSchema(
                old_password="old123456",
                new_password="new123456",
                confirm_password="new123456",
                extra_field="should_fail",
            )


# ==================== UserModel 密码方法测试 ====================

class TestUserModelPassword:
    """UserModel 密码设置与校验。"""

    def _create_user(self, password: str = "initial123") -> UserModel:
        """创建测试用户（不持久化到数据库）。"""
        return UserModel(
            username="testuser",
            email="test@example.com",
            realname="测试用户",
            password=password,
        )

    def test_password_is_hashed_on_creation(self):
        """创建用户时密码应被哈希，不存储明文。"""
        user = self._create_user("mypassword123")
        assert user._password != "mypassword123"
        assert user._password.startswith("$")  # argon2 哈希以 $ 开头

    def test_password_setter_hashes(self):
        """通过 setter 设置密码应被哈希。"""
        user = self._create_user()
        user.password = "newpassword456"
        assert user._password != "newpassword456"
        assert user._password.startswith("$")

    def test_password_getter_returns_hash(self):
        """password getter 返回哈希值，不是明文。"""
        user = self._create_user("mypassword123")
        assert user.password == user._password
        assert user.password != "mypassword123"

    def test_check_password_correct(self):
        """正确密码应通过校验。"""
        user = self._create_user("correct_password")
        assert user.check_password("correct_password") is True

    def test_check_password_incorrect(self):
        """错误密码应校验失败。"""
        user = self._create_user("correct_password")
        assert user.check_password("wrong_password") is False

    def test_check_password_after_change(self):
        """修改密码后，旧密码失效，新密码有效。"""
        user = self._create_user("old_password")
        user.password = "new_password"
        assert user.check_password("old_password") is False
        assert user.check_password("new_password") is True

    def test_password_hash_differs_each_time(self):
        """相同明文密码两次哈希结果应不同（加盐）。"""
        user1 = self._create_user("same_password")
        user2 = self._create_user("same_password")
        assert user1._password != user2._password

    def test_password_not_plain_text_in_attributes(self):
        """属性中不应包含明文密码。"""
        user = self._create_user("secret123")
        # SQLAlchemy 模型的 __dict__ 中应包含 _password（哈希），不包含明文
        assert "_password" in user.__dict__
        assert user.__dict__["_password"] != "secret123"
        assert "password" not in user.__dict__ or user.__dict__.get("password") != "secret123"


# ==================== 密码修改逻辑测试 ====================

class TestChangePasswordLogic:
    """密码修改业务逻辑（纯函数级别）。"""

    def test_change_password_success(self):
        """正常修改密码流程。"""
        user = UserModel(
            username="testuser",
            email="test@example.com",
            realname="测试用户",
            password="old_password",
        )

        # 验证旧密码
        assert user.check_password("old_password") is True

        # 修改密码
        user.password = "new_password"

        # 验证新密码
        assert user.check_password("new_password") is True
        assert user.check_password("old_password") is False

    def test_change_password_same_as_old(self):
        """新密码与旧密码相同的情况（业务上允许，但应提醒用户）。"""
        user = UserModel(
            username="testuser",
            email="test@example.com",
            realname="测试用户",
            password="same_password",
        )

        # 即使相同也应重新哈希
        old_hash = user._password
        user.password = "same_password"
        new_hash = user._password

        # 哈希值应不同（因为重新哈希）
        assert old_hash != new_hash
        assert user.check_password("same_password") is True

    def test_change_password_multiple_times(self):
        """多次修改密码。"""
        user = UserModel(
            username="testuser",
            email="test@example.com",
            realname="测试用户",
            password="password_v1",
        )

        user.password = "password_v2"
        assert user.check_password("password_v2") is True
        assert user.check_password("password_v1") is False

        user.password = "password_v3"
        assert user.check_password("password_v3") is True
        assert user.check_password("password_v2") is False
        assert user.check_password("password_v1") is False


# ==================== 边界情况测试 ====================

class TestPasswordEdgeCases:
    """密码相关边界情况。"""

    def test_password_exactly_6_chars(self):
        """密码最小长度 6 位。"""
        user = UserModel(
            username="testuser",
            email="test@example.com",
            realname="测试用户",
            password="a" * 6,
        )
        assert user.check_password("a" * 6) is True

    def test_password_exactly_15_chars(self):
        """密码最大长度 15 位。"""
        user = UserModel(
            username="testuser",
            email="test@example.com",
            realname="测试用户",
            password="a" * 15,
        )
        assert user.check_password("a" * 15) is True

    def test_password_with_special_chars(self):
        """密码包含特殊字符。"""
        special = "p@ssw0rd!#$%"
        user = UserModel(
            username="testuser",
            email="test@example.com",
            realname="测试用户",
            password=special,
        )
        assert user.check_password(special) is True

    def test_password_with_unicode(self):
        """密码包含中文字符。"""
        unicode_pwd = "密码123456"
        user = UserModel(
            username="testuser",
            email="test@example.com",
            realname="测试用户",
            password=unicode_pwd,
        )
        assert user.check_password(unicode_pwd) is True
