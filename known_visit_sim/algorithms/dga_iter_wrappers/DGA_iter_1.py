from known_visit_sim.algorithms.DGA import DGAAllocator as BaseDGAAllocator

class DGAIter1Allocator(BaseDGAAllocator):
    name = "DGA_iter_1"
    DGA_ITERATIONS_PER_TRIGGER = 1
