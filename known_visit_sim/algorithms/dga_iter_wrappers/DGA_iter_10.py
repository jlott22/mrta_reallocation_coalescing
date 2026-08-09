from known_visit_sim.algorithms.DGA import DGAAllocator as BaseDGAAllocator

class DGAIter10Allocator(BaseDGAAllocator):
    name = "DGA_iter_10"
    DGA_ITERATIONS_PER_TRIGGER = 10
