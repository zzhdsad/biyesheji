import bcrypt
print("bcrypt version:", bcrypt.__version__)
hashed = bcrypt.hashpw(b"secret123", bcrypt.gensalt(rounds=10))
print("hash:", hashed[:50])
ok = bcrypt.checkpw(b"secret123", hashed)
print("verify:", ok)
