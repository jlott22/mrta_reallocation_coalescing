from known_visit_sim.algorithms.DGA import DGAAllocator as BaseDGAAllocator

class DGAIter25Allocator(BaseDGAAllocator):
    name = "DGA_iter_25"
    DGA_ITERATIONS_PER_TRIGGER = 25
