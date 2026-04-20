#!/usr/bin/env python
"""
Password hash generator for Klovent.

Usage:
    python hash_password.py

Prompts for a password and outputs a scrypt-hashed version suitable for
storing in the MongoDB employees collection.
"""

from werkzeug.security import generate_password_hash
import getpass


def main():
    print("Klovent Password Hash Generator")
    print("-" * 40)
    password = getpass.getpass("Enter password: ")
    confirm = getpass.getpass("Confirm password: ")
    
    if password != confirm:
        print("❌ Passwords do not match.")
        return
    
    if not password:
        print("❌ Password cannot be empty.")
        return
    
    hashed = generate_password_hash(password, method="scrypt")
    print("\n✓ Hash generated successfully:")
    print("-" * 40)
    print(hashed)
    print("-" * 40)
    print("\nCopy this hash and use it to update the MongoDB 'password' field.")


if __name__ == "__main__":
    main()
