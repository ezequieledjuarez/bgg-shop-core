import string


def has_required_registration_password_composition(password):
    if not isinstance(password, str):
        return False
    return (
        any("A" <= character <= "Z" for character in password)
        and any("a" <= character <= "z" for character in password)
        and any("0" <= character <= "9" for character in password)
        and any(character in string.punctuation for character in password)
    )
