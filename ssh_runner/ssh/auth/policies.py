"""Authentication modes own their OpenSSH options and allowed challenges."""

from ...core.errors import ToolError


class AuthPolicy:
    method = "keyboard-interactive"
    wrapped_prompt = True

    def options(self, mux_only=False):
        def enabled(method):
            return "yes" if self.method == method and not mux_only else "no"

        return [
            "BatchMode=" + ("yes" if mux_only or self.method == "publickey" else "no"),
            "PreferredAuthentications=" + self.method,
            "PubkeyAuthentication=" + enabled("publickey"),
            "PasswordAuthentication=" + enabled("password"),
            "KbdInteractiveAuthentication=" + enabled("keyboard-interactive"),
        ]

    def challenge(self, config):
        return config.password_prompt or "Password:"

    def answer(self, config, prompt, prompt_host=None):
        challenge = self.challenge(config).strip()
        accepted = {challenge}
        if self.wrapped_prompt:
            accepted.add(
                "(%s@%s) %s" % (config.user, prompt_host or config.host, challenge)
            )
        if prompt.strip() not in accepted:
            raise ToolError(
                "遇到未允许的密码提示，已拒绝自动应答；请另行使用普通 SSH 处理。"
            )
        return config.password

    def diagnose_bootstrap(self, result):
        return result


class PasswordPolicy(AuthPolicy):
    method = "password"
    wrapped_prompt = False

    def challenge(self, config):
        return config.password_prompt or "%s@%s's password:" % (
            config.user,
            config.host,
        )


class KeyboardInteractivePolicy(AuthPolicy):
    pass


class KeyPolicy(AuthPolicy):
    method = "publickey"

    def answer(self, config, prompt, prompt_host=None):
        raise ToolError("密钥认证不自动应答密码提示。")


POLICIES = {
    "password": PasswordPolicy(),
    "keyboard-interactive": KeyboardInteractivePolicy(),
    "key": KeyPolicy(),
}


def get_policy(mode):
    return POLICIES[mode]
