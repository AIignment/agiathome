namespace AGIHome.Hadamard

def dotUpTo (n : Nat) (u v : Fin n → Int) : (m : Nat) → m ≤ n → Int
  | 0, _ => 0
  | m + 1, h => dotUpTo n u v m (Nat.le_of_succ_le h) + u ⟨m, h⟩ * v ⟨m, h⟩

def dot (n : Nat) (u v : Fin n → Int) : Int :=
  dotUpTo n u v n (Nat.le_refl n)

def IsHadamard (n : Nat) (H : Fin n → Fin n → Int) : Prop :=
  (∀ i j, H i j = 1 ∨ H i j = -1) ∧
  (∀ i j, dot n (H i) (H j) = if i = j then (n : Int) else 0)

instance (n : Nat) (H : Fin n → Fin n → Int) : Decidable (IsHadamard n H) := by
  unfold IsHadamard; infer_instance

def HadamardConjecture : Prop :=
  ∀ k : Nat, 0 < k → ∃ H : Fin (4 * k) → Fin (4 * k) → Int, IsHadamard (4 * k) H

def sylvester : (t : Nat) → Fin (2 ^ t) → Fin (2 ^ t) → Int
  | 0, _, _ => 1
  | t + 1, i, j =>
    let n := 2 ^ t
    let hi := i.val / n
    let hj := j.val / n
    let i' : Fin n := ⟨i.val % n, Nat.mod_lt _ (Nat.two_pow_pos t)⟩
    let j' : Fin n := ⟨j.val % n, Nat.mod_lt _ (Nat.two_pow_pos t)⟩
    (if hi = 1 ∧ hj = 1 then -1 else 1) * sylvester t i' j'

theorem hadamard_order_1 : IsHadamard 1 (sylvester 0) := by decide
theorem hadamard_order_2 : IsHadamard 2 (sylvester 1) := by decide
theorem hadamard_order_4 : IsHadamard 4 (sylvester 2) := by decide
theorem hadamard_order_8 : IsHadamard 8 (sylvester 3) := by decide

theorem hadamard_conjecture_k1 :
    ∃ H : Fin (4 * 1) → Fin (4 * 1) → Int, IsHadamard (4 * 1) H :=
  ⟨sylvester 2, hadamard_order_4⟩

theorem hadamard_conjecture_k2 :
    ∃ H : Fin (4 * 2) → Fin (4 * 2) → Int, IsHadamard (4 * 2) H :=
  ⟨sylvester 3, hadamard_order_8⟩

theorem not_hadamard_order_3 : ∀ H : Fin 3 → Fin 3 → Int, IsHadamard 3 H → False := by
  intro H ⟨hpm, horth⟩
  have h01 := horth 0 1
  simp only [dot, dotUpTo] at h01
  rcases hpm 0 0 with a | a <;> rcases hpm 0 1 with b | b <;> rcases hpm 0 2 with c | c <;>
  rcases hpm 1 0 with d | d <;> rcases hpm 1 1 with e | e <;> rcases hpm 1 2 with f | f <;>
  simp_all <;> omega

end AGIHome.Hadamard
